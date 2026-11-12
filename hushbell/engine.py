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
from hushbell.store import Store
from hushbell.triage.dedupe import WINDOW_SECONDS, find_duplicate
from hushbell.triage.rules import SafetyRules
from hushbell.voice import Voice

log = logging.getLogger(__name__)

STATUS_APPROVED = "approved"
CLOSED_STATUSES = {STATUS_DONE, STATUS_SKIPPED, STATUS_FAILED, STATUS_LATER}
PICKUP_LABELS = {"fever", "vomiting", "sick", "injury", "pickup now", "early dismissal"}


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
                 voice: Voice | None, checkpointer, executor: Executor | None = None):
        self.settings = settings
        self.store = store
        self.classifier = classifier
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

    def resume(self, item_id: str, approved: bool, via: str, reply_text: str | None = None) -> bool:
        """The parent answered (keypress or page). Returns False if the item was not waiting.

        `reply_text` lets the parent change the wording of a reply before it is sent.
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
        if item.action_type not in ("reply", "open_link"):
            return False
        if not self.store.transition(item_id, {STATUS_FAILED}, STATUS_APPROVED):
            return False
        self.store.add_event("retry", "Trying again on the phone", item_id)
        self.executor.submit(self._perform, item_id)
        return True

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
            },
            "setup": {
                "calls": self.settings.calls_enabled,
                "phone_actions": getattr(self.phone, "name", "unknown"),
                "reading_phone": self.settings.read_phone,
                "phone_health": self.health.get("phone", "off"),
                "demo": self.settings.demo_mode,
                "digest_time": self.settings.digest_time,
            },
            "digest": self.digest(since=day_start),
        }

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
        self.store.update_item(
            item.id, category=decision.category, reason=decision.reason, summary=decision.summary,
            child=decision.child, due=decision.due, action_type=decision.action_type,
            reply_text=decision.reply_text, url=decision.url, instructions=decision.instructions,
            rule_hit=decision.rule_hit, sorted_by=decision.sorted_by,
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
        item = self._item(state)
        return "act" if item.action_type in ("reply", "open_link") else "mark_seen"

    def _node_act(self, state: FlowState) -> FlowState:
        self._perform(state["item_id"])
        return {}

    def _perform(self, item_id: str) -> None:
        """Do the approved action on the phone. Runs at most once per approval."""
        if not self.store.transition(item_id, {STATUS_APPROVED}, STATUS_ACTING):
            return  # already done by an earlier run
        item = self.store.get_item(item_id)
        what = "Replying in " + item.app if item.action_type == "reply" else "Filling in the form"
        self.store.add_event("acting", f"{what} on the parent's phone", item.id)
        try:
            result = self.phone.perform(item)
        except UnsafeAction as exc:
            self.store.update_item(item.id, status=STATUS_FAILED, result=str(exc))
            self.store.add_event("stopped", str(exc), item.id)
            return
        except Exception as exc:  # noqa: BLE001 - a phone problem must not take the engine down
            log.exception("Phone action failed for %s", item.id)
            self.store.update_item(item.id, status=STATUS_FAILED, result=f"The phone step failed: {str(exc)[:200]}")
            self.store.add_event("failed", f"{what} did not finish: {str(exc)[:160]}", item.id)
            return
        if result.ok:
            self.store.update_item(item.id, status=STATUS_DONE, result=result.detail)
            self.store.add_event("done", f"{what}: done. {result.detail}", item.id)
        else:
            self.store.update_item(item.id, status=STATUS_FAILED, result=result.detail)
            self.store.add_event("failed", f"{what} did not finish: {result.detail}", item.id)

    def _node_mark_seen(self, state: FlowState) -> FlowState:
        item = self._item(state)
        self.store.update_item(item.id, status=STATUS_DONE, result="Marked as seen")
        self.store.add_event("done", "Marked as seen", item.id)
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


def _short(text: str, n: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _mask(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"the number ending {digits[-4:]}" if len(digits) >= 4 else "the parent"


def _day_start() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, lt.tm_wday, lt.tm_yday, lt.tm_isdst))
