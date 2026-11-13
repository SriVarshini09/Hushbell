"""The Hushbell engine: one LangGraph run per school item.

    triage ──urgent────► call_parent ──► await_ok ──yes──► act
       │                                    ▲     └─no───► skip
       ├──needs_you──► ask_on_page ─────────┘
       └──later──────► file_later

`await_ok` pauses the run with LangGraph's interrupt(). It resumes when the parent
presses 1 on the call or taps Approve on the Hushbell page. The phone action runs
only after that, in its own node, so a resumed run never repeats a side effect.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from hushbell.config import Settings
from hushbell.models import (
    OPEN_STATUSES, STATUS_ACTING, STATUS_CALLING, STATUS_DONE, STATUS_FAILED, STATUS_LATER,
    STATUS_NEW, STATUS_SKIPPED, STATUS_SORTING, STATUS_WAITING, Decision, IncomingMessage, Item,
)
from hushbell.phone import PhoneBackend, UnsafeAction
from hushbell.plan import PICKUP_LABELS, build_steps, parse_date, parse_time, spoken_list
from hushbell.store import Store
from hushbell.triage.dedupe import WINDOW_SECONDS, find_duplicate
from hushbell.triage.rules import SafetyRules
from hushbell.voice import Voice

log = logging.getLogger(__name__)

STATUS_APPROVED = "approved"
CLOSED_STATUSES = {STATUS_DONE, STATUS_SKIPPED, STATUS_FAILED, STATUS_LATER}


class FlowState(TypedDict, total=False):
    item_id: str
    category: str
    approved: bool
    via: str


class InlineExecutor(Executor):
    """Runs work immediately on the calling thread (tests, scripts)."""

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[override]
        from concurrent.futures import Future

        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - surfaced through the future
            future.set_exception(exc)
        return future


class Engine:
    def __init__(self, settings: Settings, store: Store, classifier, phone: PhoneBackend,
                 voice: Voice | None, checkpointer, executor: Executor | None = None, assistant=None):
        self.settings = settings
        self.store = store
        self.classifier = classifier
        self.assistant = assistant
        self.phone = phone
        self.voice = voice
        self.rules = SafetyRules(settings.children, settings.extra_urgent_words)
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="hushbell")
        self._ingest_lock = threading.Lock()
        self.health: dict[str, str] = {"phone": "off" if not settings.read_phone else "starting"}
        self.graph = self._build_graph().compile(checkpointer=checkpointer)

    # ------------------------------------------------------------------ intake

    def ingest(self, msg: IncomingMessage) -> tuple[Item, bool]:
        """Store a new school message. Returns (item, is_new). Duplicates join the earlier item."""
        with self._ingest_lock:
            candidates = self.store.recent_items(WINDOW_SECONDS)
            if self.rules.check(f"{msg.sender}\n{msg.text}"):
                # A repeated urgent message only joins an alert that is still in progress.
                # Once that alert is closed, the repeat is a follow-up and must ring again.
                candidates = [i for i in candidates if i.status not in CLOSED_STATUSES]
            duplicate = find_duplicate(msg.text, candidates)
            if duplicate:
                sources = duplicate.sources + ([msg.app] if msg.app not in duplicate.sources else [])
                item = self.store.update_item(duplicate.id, sources=sources,
                                              duplicate_count=duplicate.duplicate_count + 1)
                self.store.add_message(duplicate.id, msg.app, msg.package, msg.sender, msg.text, msg.source)
                self.store.add_event("merged", f"Same news from {msg.app} ({msg.sender}) joined an earlier message",
                                     duplicate.id)
                return item, False
            item = self.store.create_item(msg.app, msg.sender, msg.text)
            self.store.add_message(item.id, msg.app, msg.package, msg.sender, msg.text, msg.source)
            self.store.add_event("received", f"{msg.app} · {msg.sender}: {_short(msg.text)}", item.id)
        self.executor.submit(self._run, item.id, {"item_id": item.id})
        return item, True

    # ------------------------------------------------------------- the parent

    def resume(self, item_id: str, approved: bool, via: str, reply_text: str | None = None,
               skip: list[str] | None = None) -> bool:
        """The parent answered (keypress or page). Returns False if the item was not waiting.

        `reply_text` lets the parent change the wording of a reply before it is sent.
        `skip` lists step ids the parent unticked on the page.
        """
        target = STATUS_APPROVED if approved else STATUS_SKIPPED
        if not self.store.transition(item_id, OPEN_STATUSES, target):
            return False
        if approved and reply_text is not None:
            wanted = " ".join(reply_text.split())[:300]
            item = self.store.get_item(item_id)
            if item and item.action_type == "reply" and wanted and wanted != item.reply_text:
                self.store.update_item(item_id, reply_text=wanted)
                self.store.add_event("edited", "You changed the reply before sending", item_id)
        if approved and skip:
            item = self.store.get_item(item_id)
            steps = [dict(s, enabled=False, status="skipped") if s["id"] in skip else s for s in item.steps]
            self.store.update_item(item_id, steps=steps)
        if approved and via == "call":
            # On the call, Twilio connects the parent to the caller straight away (see web/app.py).
            item = self.store.get_item(item_id)
            if any(s["type"] == "call_back" for s in item.steps):
                steps = [dict(s, status="done", detail="Connected you to them on the call.")
                         if s["type"] == "call_back" and s.get("enabled", True) else s for s in item.steps]
                self.store.update_item(item_id, steps=steps)
        verb = "approved" if approved else "said they will handle it"
        how = "on the call" if via == "call" else "on the Hushbell page"
        self.store.add_event("answer", f"Parent {verb} {how}", item_id)
        self.executor.submit(self._run, item_id, Command(resume={"approved": approved, "via": via}))
        return True

    def call_status(self, item_id: str, status: str) -> None:
        """Twilio's final call status. Redial once if the call was missed."""
        item = self.store.get_item(item_id)
        if not item or item.status != STATUS_CALLING:
            return
        if status in {"no-answer", "busy", "failed", "canceled"}:
            if item.call_attempts < 2 and self.voice:
                self.store.add_event(
                    "no answer",
                    f"No answer. Calling again in {self.settings.redial_seconds} s "
                    "(a second call soon after the first gets through Do Not Disturb on most phones)",
                    item_id,
                )
                timer = threading.Timer(self.settings.redial_seconds, self._redial, args=(item_id,))
                timer.daemon = True
                timer.start()
                return
            self.store.update_item(item_id, status=STATUS_WAITING)
            self.store.add_event("no answer", "No answer after 2 calls. Waiting on the Hushbell page", item_id)
        elif status == "completed":
            # The call ended without a keypress.
            self.store.transition(item_id, {STATUS_CALLING}, STATUS_WAITING)
            self.store.add_event("call ended", "Call ended without an answer. Waiting on the Hushbell page", item_id)

    def _redial(self, item_id: str) -> None:
        item = self.store.get_item(item_id)
        if item and item.status == STATUS_CALLING:
            self._place_call(item)

    def retry(self, item_id: str) -> bool:
        """Try a failed item again: sort it again, or redo the phone action the parent already approved."""
        item = self.store.get_item(item_id)
        if not item or item.status != STATUS_FAILED:
            return False
        if not item.category:
            if not self.store.transition(item_id, {STATUS_FAILED}, STATUS_NEW):
                return False
            self.store.update_item(item_id, result="")
            self.store.add_event("retry", "Sorting this message again", item_id)
            self.executor.submit(self._run, item_id, {"item_id": item_id})
            return True
        if not _to_do(item.steps):
            return False
        if not self.store.transition(item_id, {STATUS_FAILED}, STATUS_APPROVED):
            return False
        self.store.add_event("retry", "Trying the unfinished steps again on the phone", item_id)
        self.executor.submit(self._perform, item_id)
        return True

    def run_step_now(self, item_id: str, step_id: str) -> bool:
        """Do one step of a message that didn't need approval, like "Add to calendar" on a later item."""
        item = self.store.get_item(item_id)
        if not item or item.status not in {STATUS_LATER, STATUS_DONE, STATUS_SKIPPED}:
            return False
        step = next((s for s in item.steps if s["id"] == step_id), None)
        if not step or step["type"] in ("reply", "open_link") or step.get("status") == "done":
            return False
        self.store.add_event("asked", f"You asked Hushbell to {step['say']}", item_id)
        self.executor.submit(self._run_one_step, item_id, step_id)
        return True

    def _run_one_step(self, item_id: str, step_id: str) -> None:
        item = self.store.get_item(item_id)
        step = next((s for s in item.steps if s["id"] == step_id), None)
        if step is None:
            return
        ok, detail = self._do_step(item, step)
        steps = [dict(s, enabled=True, status="done" if ok else "failed", detail=detail) if s["id"] == step_id else s
                 for s in self.store.get_item(item_id).steps]
        self.store.update_item(item_id, steps=steps)
        self.store.add_event("done" if ok else "failed", detail, item_id)

    def ask(self, question: str) -> str:
        """Answer a question about recent school messages ("What do I need this week?")."""
        if self.assistant is None:
            return "Asking needs a Claude key. Run `hushbell init` to add one."
        items = self.store.list_items(since=time.time() - 30 * 24 * 3600, limit=200)
        return self.assistant.answer(question, items)

    def reset(self) -> None:
        """Start over: clear all messages and the log (used between demo rehearsals)."""
        with self._ingest_lock:
            self.store.clear_history()
            self.store.add_event("start over", "All messages cleared. Ready for a fresh run")

    def recover(self) -> int:
        """After a restart, pick up anything that was in progress. Returns how many items were touched."""
        touched = 0
        for item in self.store.list_items(since=time.time() - 2 * 24 * 3600, limit=500):
            if item.status in {STATUS_NEW, STATUS_SORTING}:
                self.store.add_event("restart", "Hushbell restarted. Sorting this message now", item.id)
                self.executor.submit(self._run, item.id, {"item_id": item.id})
            elif item.status == STATUS_CALLING:
                self.store.update_item(item.id, status=STATUS_WAITING)
                self.store.add_event("restart", "Hushbell restarted during the call. Waiting on the Hushbell page",
                                     item.id)
            elif item.status in {STATUS_APPROVED, STATUS_ACTING}:
                self.store.update_item(
                    item.id, status=STATUS_FAILED,
                    result="Hushbell restarted before this finished. Check the app, then press Try again if needed.")
                self.store.add_event("restart", "Hushbell restarted before the phone finished this", item.id)
            else:
                continue
            touched += 1
        return touched

    # ------------------------------------------------------------------ state

    def state(self) -> dict[str, Any]:
        day_start = _day_start()
        items = self.store.list_items(since=time.time() - 3 * 24 * 3600, limit=150)
        today = [i for i in items if i.created_at >= day_start]
        return {
            "items": [i.to_public() for i in items],
            "events": self.store.list_events(limit=40),
            "counts": {
                "messages_today": self.store.count_messages(since=day_start),
                "urgent_today": sum(1 for i in today if i.category == "urgent"),
                "needs_you": sum(1 for i in items if i.status in OPEN_STATUSES),
                "later_today": sum(1 for i in today if i.category == "later"),
                "done_today": sum(1 for i in today if i.status == STATUS_DONE),
                "calls_today": sum(1 for i in today if i.call_attempts > 0),
                "taps_today": sum(1 for e in self.store.list_events(limit=500, since=day_start)
                                  if e["kind"] == "answer" and "page" in e["detail"]),
                "keypresses_today": sum(1 for e in self.store.list_events(limit=500, since=day_start)
                                        if e["kind"] == "answer" and "call" in e["detail"]),
            },
            "setup": {
                "calls": self.settings.calls_enabled,
                "phone_actions": getattr(self.phone, "name", "unknown"),
                "reading_phone": self.settings.read_phone,
                "phone_health": self.health.get("phone", "off"),
                "demo": self.settings.demo_mode,
                "digest_time": self.settings.digest_time,
                "parent_name": self.settings.parent_name,
                "partner_name": self.settings.partner_name,
            },
            "now": time.time(),
            "digest": self.digest(since=day_start),
            "today": time.strftime("%Y-%m-%d"),
            "coming_up": self.coming_up(),
            "can_ask": self.assistant is not None,
        }

    def coming_up(self, days: int = 21) -> list[dict]:
        """Dated school events and deadlines from today on, soonest first (a family calendar view)."""
        today = time.strftime("%Y-%m-%d")
        items = self.store.list_items(since=time.time() - 30 * 24 * 3600, limit=500)
        rows = []
        for i in items:
            day = parse_date(i.event_date)
            if not day or i.event_date < today or (day - parse_date(today)).days > days:
                continue
            if i.category == "urgent":
                continue  # urgent messages sit at the top of the page, not on the calendar view
            calendar = next((s for s in i.steps if s["type"] == "add_calendar"), None)
            rows.append({
                "item_id": i.id, "date": i.event_date, "time": i.event_time,
                "title": i.event_title or i.summary or i.text, "child": i.child, "category": i.category,
                "calendar_step": calendar["id"] if calendar else "",
                "on_calendar": bool(calendar and calendar.get("status") == "done"),
                "can_add": bool(calendar) and i.status in {STATUS_LATER, STATUS_DONE, STATUS_SKIPPED}
                           and calendar.get("status") != "done",
            })
        return sorted(rows, key=lambda r: (r["date"], r["time"] or "99:99"))

    def digest(self, since: float) -> dict[str, Any]:
        items = self.store.list_items(since=since, limit=500)
        return {
            "messages": self.store.count_messages(since=since),
            "calls": sum(1 for i in items if i.call_attempts > 0),
            "handled": [f"{i.child + ': ' if i.child else ''}{i.summary or _short(i.text)}" for i in items
                        if i.status == STATUS_DONE],
            "later": [f"{i.sender}: {i.summary or _short(i.text)}" for i in items if i.status == STATUS_LATER],
            "open": [f"{i.sender}: {i.summary or _short(i.text)}" for i in items if i.status in OPEN_STATUSES],
        }

    # ---------------------------------------------------------- graph pieces

    def _run(self, item_id: str, payload) -> None:
        config = {"configurable": {"thread_id": item_id}}
        try:
            self.graph.invoke(payload, config)
        except Exception as exc:  # noqa: BLE001 - keep the engine alive, record what broke
            log.exception("Run for item %s failed", item_id)
            self.store.update_item(item_id, status=STATUS_FAILED, result=str(exc)[:300])
            self.store.add_event("error", f"Something went wrong: {str(exc)[:200]}", item_id)

    def _build_graph(self) -> StateGraph:
        g = StateGraph(FlowState)
        g.add_node("triage", self._node_triage)
        g.add_node("call_parent", self._node_call_parent)
        g.add_node("ask_on_page", self._node_ask_on_page)
        g.add_node("await_ok", self._node_await_ok)
        g.add_node("act", self._node_act)
        g.add_node("mark_seen", self._node_mark_seen)
        g.add_node("skip", self._node_skip)
        g.add_node("file_later", self._node_file_later)
        g.add_edge(START, "triage")
        g.add_conditional_edges("triage", lambda s: s["category"],
                                {"urgent": "call_parent", "needs_you": "ask_on_page", "later": "file_later"})
        g.add_edge("call_parent", "await_ok")
        g.add_edge("ask_on_page", "await_ok")
        g.add_conditional_edges("await_ok", self._route_answer,
                                {"act": "act", "mark_seen": "mark_seen", "skip": "skip"})
        g.add_edge("act", END)
        g.add_edge("mark_seen", END)
        g.add_edge("skip", END)
        g.add_edge("file_later", END)
        return g

    def _node_triage(self, state: FlowState) -> FlowState:
        item = self._item(state)
        self.store.update_item(item.id, status=STATUS_SORTING)
        decision = self.decide(item)
        labels = [x.strip() for x in decision.rule_hit.split(",") if x.strip()]
        steps = build_steps(item, decision, self.settings, labels)
        self.store.update_item(
            item.id, category=decision.category, reason=decision.reason, summary=decision.summary,
            child=decision.child, due=decision.due, action_type=decision.action_type,
            reply_text=decision.reply_text, url=decision.url, instructions=decision.instructions,
            rule_hit=decision.rule_hit, sorted_by=decision.sorted_by, steps=steps,
            event_date=decision.date if parse_date(decision.date) else "",
            event_time=decision.time if parse_time(decision.time) else "",
            event_title=decision.event_title.strip()[:80],
        )
        label = {"urgent": "URGENT", "needs_you": "NEEDS YOU", "later": "LATER"}[decision.category]
        why = f"safety words: {decision.rule_hit}" if decision.rule_hit else decision.reason
        self.store.add_event("sorted", f"{label} · {why}", item.id)
        return {"category": decision.category}

    def decide(self, item: Item) -> Decision:
        hit = self.rules.check(f"{item.sender}\n{item.text}")
        try:
            decision = self.classifier.classify(item.app, item.sender, item.text, also_seen_in=item.sources[1:])
        except Exception as exc:  # noqa: BLE001 - any sorting failure falls back to the safe default
            log.warning("Sorting fell back to rules for %s: %s", item.id, exc)
            self.store.add_event("sorting", f"Claude was unavailable ({exc}). Used the safe default.", item.id)
            decision = Decision(
                category="urgent" if hit else "needs_you",
                reason="Couldn't sort this automatically, so it's waiting for you",
                summary=f"{item.sender} in {item.app} says: {_short(item.text, 160)}",
                sorted_by="fallback",
            )
        if hit:
            decision.category = "urgent"
            decision.rule_hit = hit.text
            decision.sorted_by = "rules" if decision.sorted_by == "fallback" else "rules+claude"
            if decision.action_type != "reply" or not decision.reply_text:
                pickup = any(label in PICKUP_LABELS for label in hit.labels)
                decision.action_type = "reply"
                decision.reply_text = ("Thank you for letting me know. I'm on my way." if pickup
                                       else "Thank you for letting me know. Please call me as soon as you can.")
        return decision

    def _node_call_parent(self, state: FlowState) -> FlowState:
        self._place_call(self._item(state))
        return {}

    def _place_call(self, item: Item) -> None:
        if not self.voice:
            self.store.update_item(item.id, status=STATUS_WAITING)
            self.store.add_event("no call", "Calls are not set up, so this is waiting at the top of the Hushbell page",
                                 item.id)
            return
        try:
            self.voice.place_call(item.id)
        except Exception as exc:  # noqa: BLE001 - a failed call must not lose the item
            log.exception("Call failed for %s", item.id)
            self.store.update_item(item.id, status=STATUS_WAITING)
            self.store.add_event("call failed", f"Could not place the call ({str(exc)[:160]}). Waiting on the page",
                                 item.id)
            return
        self.store.update_item(item.id, status=STATUS_CALLING, call_attempts=item.call_attempts + 1)
        self.store.add_event("calling", f"Calling {_mask(self.settings.parent_phone)} through Do Not Disturb",
                             item.id)

    def _node_ask_on_page(self, state: FlowState) -> FlowState:
        item = self._item(state)
        self.store.update_item(item.id, status=STATUS_WAITING)
        self.store.add_event("needs you", "Waiting for your OK on the Hushbell page", item.id)
        return {}

    def _node_await_ok(self, state: FlowState) -> FlowState:
        answer = interrupt({"item_id": state["item_id"], "question": "approve?"})
        return {"approved": bool(answer.get("approved")), "via": str(answer.get("via", "page"))}

    def _route_answer(self, state: FlowState) -> str:
        if not state.get("approved"):
            return "skip"
        return "act" if _to_do(self._item(state).steps) else "mark_seen"

    def _node_act(self, state: FlowState) -> FlowState:
        self._perform(state["item_id"])
        return {}

    def _perform(self, item_id: str) -> None:
        """Do the approved steps on the phone, in order. Runs at most once per approval.

        Each step's result is saved as it finishes, so the page shows progress, and
        "Try again" only redoes the steps that didn't finish.
        """
        if not self.store.transition(item_id, {STATUS_APPROVED}, STATUS_ACTING):
            return  # already done by an earlier run
        item = self.store.get_item(item_id)
        steps = [dict(s) for s in item.steps]
        todo = [s for s in steps if s.get("enabled", True) and s.get("status") != "done"]
        self.store.add_event("acting", f"Working on your phone: {spoken_list(todo)}", item.id)
        failures: list[str] = []
        for step in todo:
            ok, detail = self._do_step(self.store.get_item(item_id), step)
            step["status"], step["detail"] = ("done" if ok else "failed"), detail
            self.store.update_item(item_id, steps=steps)
            self.store.add_event("done" if ok else "failed", detail, item.id)
            if not ok:
                failures.append(detail.rstrip("."))
        finished = [s["detail"] for s in steps if s.get("status") == "done" and s.get("detail")]
        if failures:
            self.store.update_item(item_id, status=STATUS_FAILED,
                                   result=". ".join(failures) + ". Check the phone, then press Try again.")
        else:
            self.store.update_item(item_id, status=STATUS_DONE, result=" ".join(finished) or "Done.")

    def _do_step(self, item: Item, step: dict) -> tuple[bool, str]:
        """Run one step. Returns (ok, a sentence for the parent)."""
        try:
            if step["type"] in ("reply", "open_link"):
                result = self.phone.perform(item)
                if result.ok:
                    what = (f"Reply sent in {item.app}." if step["type"] == "reply" else "Form submitted.")
                    return True, what if "Dry run" not in result.detail else f"{what} (dry run)"
                return False, f"{step['label']} didn't finish: {result.detail}"
            result = self.phone.perform_step(item, step)
            return result.ok, (result.detail if result.ok else f"{step['label']} didn't finish: {result.detail}")
        except UnsafeAction as exc:
            return False, f"Stopped: {exc}"
        except Exception as exc:  # noqa: BLE001 - a phone problem must not take the engine down
            log.exception("Phone step failed for %s", item.id)
            return False, f"{step['label']} didn't finish: {str(exc)[:160]}"

    def _node_mark_seen(self, state: FlowState) -> FlowState:
        item = self._item(state)
        finished = " ".join(s["detail"] for s in item.steps if s.get("status") == "done" and s.get("detail"))
        self.store.update_item(item.id, status=STATUS_DONE, result=finished or "Marked as seen")
        self.store.add_event("done", finished or "Marked as seen", item.id)
        return {}

    def _node_skip(self, state: FlowState) -> FlowState:
        item = self._item(state)
        self.store.update_item(item.id, status=STATUS_SKIPPED, result="Parent will handle it")
        return {}

    def _node_file_later(self, state: FlowState) -> FlowState:
        item = self._item(state)
        self.store.update_item(item.id, status=STATUS_LATER)
        self.store.add_event("later", f"Saved for the {self.settings.digest_time} digest", item.id)
        return {}

    def _item(self, state: FlowState) -> Item:
        item = self.store.get_item(state["item_id"])
        if item is None:
            raise LookupError(f"Item {state['item_id']} not found")
        return item

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


def _to_do(steps: list[dict]) -> list[dict]:
    """Steps that the parent left ticked and that haven't run yet."""
    return [s for s in steps if s.get("enabled", True) and s.get("status") != "done"
            and s["type"] in ("reply", "open_link", "set_alarm", "text_partner", "directions", "add_calendar",
                              "call_back")]


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _mask(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"the number ending {digits[-4:]}" if len(digits) >= 4 else "the parent"


def _day_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
