"""FastAPI app: the parent's page, a small JSON API, and the Twilio call webhooks."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from fastapi.concurrency import run_in_threadpool

from hushbell import __version__
from hushbell.engine import Engine
from hushbell.models import IncomingMessage
from hushbell.plan import CALL_APP, pretty_phone, same_phone, valid_phone
from hushbell.voice import (
    gather_twiml, inbound_check_emergency_twiml, inbound_done_twiml, inbound_greeting_twiml, needs_page,
    parent_ask_twiml, ring_test_twiml, voice_twiml,
)

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "hushbell_session"

def demo_pickup_time(now: datetime | None = None) -> str:
    """A pickup time about 90 minutes from now, on the half hour, so the demo always has a
    "time to leave" alarm to set, whatever time of day the demo runs."""
    now = now or datetime.now()
    later = now + timedelta(minutes=90)
    later = later.replace(second=0, microsecond=0)
    later += timedelta(minutes=(30 - later.minute % 30) % 30)
    hour = later.hour % 12 or 12
    suffix = "am" if later.hour < 12 else "pm"
    return f"{hour}:{later.minute:02d}{suffix}" if later.minute else f"{hour}{suffix}"


def demo_messages(children: list[str]) -> list[dict]:
    """Example school messages, about the family's own child so they read as real."""
    child = (children[0].strip() if children and children[0].strip() else "Maya")
    child = child[:1].upper() + child[1:]
    teacher = "Ms. Rivera · Room 12"
    return [
        {"app": "Remind", "sender": teacher, "text": "Bake sale Friday! Bring cookies."},
        {"app": "Remind", "sender": teacher,
         "text": "Field trip to the science museum next Friday! Permission form due TODAY: "
                 "https://forms.gle/fieldtrip12"},
        {"app": "WhatsApp", "sender": "3rd grade parents",
         "text": "Jen: Reminder: field trip permission form due today!"},
        {"app": "ClassDojo", "sender": "Mr. Chen",
         "text": f"Hi! Can {child} stay for chess club this Wednesday until 4:30?"},
        {"app": "Gmail", "sender": "Lincoln Elementary",
         "text": "Picture day is next Thursday. Retake forms are in the front office."},
        {"app": "Remind", "sender": teacher,
         "text": f"{child} has a fever (101°F) and needs to be picked up by {demo_pickup_time()}."},
    ]


class IngestBody(BaseModel):
    app: str = Field(min_length=1, max_length=60)
    sender: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)


class ApproveBody(BaseModel):
    reply_text: str | None = Field(default=None, max_length=300)
    skip: list[str] = Field(default_factory=list, max_length=10)


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=300)


FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#F4B400"/>'
    '<path d="M16 6a6 6 0 0 0-6 6v5l-2.5 3.5h17L22 17v-5a6 6 0 0 0-6-6zm-2.5 17a2.5 2.5 0 0 0 5 0z" fill="#14203A"/></svg>'
)

MAX_LOGIN_FAILURES = 8
LOGIN_WINDOW_SECONDS = 600


def create_app(engine: Engine) -> FastAPI:
    settings = engine.settings
    app = FastAPI(title="Hushbell", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    session_value = hmac.new(settings.access_token.encode(), b"hushbell-session-v1", hashlib.sha256).hexdigest()

    def authed(request: Request) -> bool:
        cookie = request.cookies.get(COOKIE, "")
        if cookie and hmac.compare_digest(cookie, session_value):
            return True
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return hmac.compare_digest(header[7:].strip(), settings.access_token)
        return False

    def require(request: Request) -> None:
        if not authed(request):
            raise HTTPException(status_code=401, detail="Sign in with your Hushbell access token.")

    async def twilio_ok(request: Request) -> dict:
        form = dict(await request.form())
        if not settings.twilio_validate:
            return form
        from twilio.request_validator import RequestValidator

        url = settings.public_base_url + request.url.path
        if request.url.query:
            url += "?" + request.url.query
        signature = request.headers.get("x-twilio-signature", "")
        if not RequestValidator(settings.twilio_auth_token).validate(url, form, signature):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")
        return form

    # ---- page -----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        page = "index.html" if authed(request) else "login.html"
        return HTMLResponse((STATIC / page).read_text(encoding="utf-8"))

    @app.get("/present", response_class=HTMLResponse)
    def present(request: Request):
        """Big-screen view for a projector: each message, what Hushbell decided, and what it did."""
        page = "present.html" if authed(request) else "login.html"
        return HTMLResponse((STATIC / page).read_text(encoding="utf-8"))

    failures: dict[str, list[float]] = {}

    @app.post("/login")
    async def login(request: Request):
        who = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in failures.get(who, []) if now - t < LOGIN_WINDOW_SECONDS]
        html = (STATIC / "login.html").read_text(encoding="utf-8")
        if len(recent) >= MAX_LOGIN_FAILURES:
            html = html.replace("<!--error-->", '<p class="error" role="alert">Too many wrong tokens. '
                                "Wait 10 minutes and try again.</p>")
            return HTMLResponse(html, status_code=429)
        form = await request.form()
        token = str(form.get("token", "")).strip()
        if not token or not hmac.compare_digest(token, settings.access_token):
            failures[who] = recent + [now]
            html = html.replace("<!--error-->", '<p class="error" role="alert">That token didn\'t match. '
                                "Check the one Hushbell printed when it started.</p>")
            return HTMLResponse(html, status_code=401)
        failures.pop(who, None)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, session_value, httponly=True, samesite="lax", max_age=30 * 24 * 3600,
                            secure=request.url.scheme == "https")
        return response

    @app.post("/logout")
    def logout():
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    @app.get("/favicon.svg")
    @app.get("/favicon.ico")
    def favicon():
        return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    # ---- API ------------------------------------------------------------

    @app.get("/api/state")
    def state(request: Request):
        require(request)
        return engine.state()

    @app.post("/api/items/{item_id}/approve")
    def approve(item_id: str, request: Request, body: ApproveBody | None = None):
        require(request)
        if not engine.resume(item_id, True, "page", reply_text=body.reply_text if body else None,
                             skip=body.skip if body else None):
            return JSONResponse({"ok": False, "detail": "This message is not waiting for you any more."}, 409)
        return {"ok": True}

    @app.post("/api/items/{item_id}/steps/{step_id}")
    def run_step(item_id: str, step_id: str, request: Request):
        """Do one step now for a message that didn't need approval ("Add to calendar")."""
        require(request)
        if not engine.run_step_now(item_id, step_id):
            return JSONResponse({"ok": False, "detail": "That can't be done for this message right now."}, 409)
        return {"ok": True}

    @app.post("/api/ask")
    def ask(body: AskBody, request: Request):
        require(request)
        return {"answer": engine.ask(body.question)}

    @app.post("/api/items/{item_id}/retry")
    def retry(item_id: str, request: Request):
        require(request)
        if not engine.retry(item_id):
            return JSONResponse({"ok": False, "detail": "There is nothing to try again for this message."}, 409)
        return {"ok": True}

    @app.post("/api/test-call")
    def test_call(request: Request):
        require(request)
        if not engine.voice or not hasattr(engine.voice, "place_test_call"):
            return JSONResponse({"ok": False, "detail": "Calls are not set up yet. Run `hushbell doctor`."}, 409)
        try:
            engine.voice.place_test_call()
        except Exception as exc:  # noqa: BLE001 - show the reason on the page
            return JSONResponse({"ok": False, "detail": f"The test call could not be placed: {str(exc)[:200]}"}, 502)
        engine.store.add_event("test call", "Test call placed. Your phone should ring, even on Do Not Disturb")
        return {"ok": True}

    @app.post("/api/items/{item_id}/skip")
    def skip(item_id: str, request: Request):
        require(request)
        if not engine.resume(item_id, False, "page"):
            return JSONResponse({"ok": False, "detail": "This message is not waiting for you any more."}, 409)
        return {"ok": True}

    @app.post("/api/ingest")
    def ingest(body: IngestBody, request: Request):
        """For notification-forwarding apps (Tasker, MacroDroid...) or tests."""
        require(request)
        item, is_new = engine.ingest(IncomingMessage(app=body.app, sender=body.sender, text=body.text, source="api"))
        return {"ok": True, "item_id": item.id, "new": is_new}

    @app.post("/api/demo/reset")
    def demo_reset(request: Request):
        """Clear every message so a rehearsal can be run again from the start."""
        require(request)
        if not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Demo mode is off (set HUSHBELL_DEMO=1).")
        engine.reset()
        return {"ok": True}

    @app.post("/api/demo/{index}")
    def demo(index: int, request: Request):
        require(request)
        if not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Demo mode is off (set HUSHBELL_DEMO=1).")
        messages = demo_messages(settings.children)
        if not 0 <= index < len(messages):
            raise HTTPException(status_code=404, detail="No such demo message.")
        msg = messages[index]
        item, is_new = engine.ingest(IncomingMessage(**msg, source="demo"))
        return {"ok": True, "item_id": item.id, "new": is_new}

    # ---- Twilio webhooks -------------------------------------------------

    @app.post("/twilio/voice/{item_id}")
    async def twilio_voice(item_id: str, request: Request, attempt: int = 1):
        await twilio_ok(request)
        item = engine.store.get_item(item_id)
        return Response(voice_twiml(item, settings.public_base_url, attempt), media_type="application/xml")

    @app.post("/twilio/gather/{item_id}")
    async def twilio_gather(item_id: str, request: Request):
        form = await twilio_ok(request)
        digit = str(form.get("Digits", "")).strip()
        item = engine.store.get_item(item_id)
        accepted = False
        if needs_page(item, digit):
            engine.store.add_event("call", "You pressed 1, but forms are approved on the page. Still waiting there",
                                   item_id)
        elif item and digit in ("1", "2"):
            accepted = engine.resume(item_id, digit == "1", "call")
        return Response(gather_twiml(digit, item, settings.public_base_url, accepted,
                                     caller_id=settings.twilio_from_number),
                        media_type="application/xml")

    # ---- someone calls Hushbell's number -----------------------------------
    # The school (or a judge playing the school nurse) calls and speaks. Hushbell writes down
    # what they said, sorts it like any school message, and rings the parent if it is urgent.
    # The parent calling from their own number gets "Ask Hushbell" by voice instead.

    pending_calls: dict[str, tuple[str, str, float]] = {}   # CallSid -> (caller, what they said, when)

    def is_parent(number: str) -> bool:
        return bool(settings.parent_phone) and same_phone(number, settings.parent_phone)

    def xml(body: str) -> Response:
        return Response(body, media_type="application/xml")

    @app.post("/twilio/inbound")
    async def twilio_inbound(request: Request, attempt: int = 1):
        form = await twilio_ok(request)
        caller = str(form.get("From", ""))
        if is_parent(caller):
            engine.store.add_event("ask", "You called Hushbell to ask a question")
            return xml(parent_ask_twiml(settings))
        if attempt == 1:
            engine.store.add_event("incoming call", f"Hushbell answered a call from {pretty_phone(caller) or 'a hidden number'}")
        return xml(inbound_greeting_twiml(settings, attempt))

    @app.post("/twilio/inbound/heard")
    async def twilio_inbound_heard(request: Request):
        form = await twilio_ok(request)
        caller = str(form.get("From", ""))
        said = " ".join(str(form.get("SpeechResult", "")).split())[:1000]
        if not said:
            return xml(inbound_greeting_twiml(settings, attempt=2))
        if engine.rules.check(said):
            take_call_message(caller, said)
            return xml(inbound_done_twiml(settings, urgent=True))
        now = time.time()
        for sid in [s for s, v in pending_calls.items() if now - v[2] > 600]:
            pending_calls.pop(sid, None)
        pending_calls[str(form.get("CallSid", ""))] = (caller, said, now)
        return xml(inbound_check_emergency_twiml(settings))

    @app.post("/twilio/inbound/emergency")
    async def twilio_inbound_emergency(request: Request):
        form = await twilio_ok(request)
        pending = pending_calls.pop(str(form.get("CallSid", "")), None)
        if pending is None:
            return xml(inbound_done_twiml(settings, urgent=False))
        caller, said, _ = pending
        urgent = str(form.get("Digits", "")).strip() == "1"
        if urgent:
            said += " (The caller pressed 1 to say this is an emergency.)"
        take_call_message(caller, said)
        return xml(inbound_done_twiml(settings, urgent=urgent))

    def take_call_message(caller: str, said: str) -> None:
        sender = caller if valid_phone(caller) else "Hidden number"
        engine.ingest(IncomingMessage(app=CALL_APP, sender=sender, text=said, source="call"))

    @app.post("/twilio/ask")
    async def twilio_ask(request: Request):
        form = await twilio_ok(request)
        if not is_parent(str(form.get("From", ""))):
            return xml(inbound_done_twiml(settings, urgent=False))
        question = " ".join(str(form.get("SpeechResult", "")).split())[:300]
        if not question:
            return xml(parent_ask_twiml(settings, answer="Sorry, I didn't catch that."))
        answer = await run_in_threadpool(engine.ask, question)
        engine.store.add_event("ask", f"You asked by phone: {question}")
        return xml(parent_ask_twiml(settings, answer=answer))

    @app.post("/twilio/test")
    async def twilio_test(request: Request):
        await twilio_ok(request)
        return Response(ring_test_twiml(), media_type="application/xml")

    @app.post("/twilio/status/{item_id}")
    async def twilio_status(item_id: str, request: Request):
        form = await twilio_ok(request)
        engine.call_status(item_id, str(form.get("CallStatus", "")))
        return Response(status_code=204)

    return app
