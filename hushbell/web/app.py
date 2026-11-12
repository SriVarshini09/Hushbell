"""FastAPI app: the parent's page, a small JSON API, and the Twilio call webhooks."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from hushbell import __version__
from hushbell.engine import Engine
from hushbell.models import IncomingMessage
from hushbell.voice import gather_twiml, ring_test_twiml, voice_twiml

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
COOKIE = "hushbell_session"

DEMO_MESSAGES = [
    {"app": "Remind", "sender": "Ms. Rivera · Room 12", "text": "Bake sale Friday! Bring cookies."},
    {"app": "Remind", "sender": "Ms. Rivera · Room 12",
     "text": "Field trip form due TODAY: https://forms.gle/fieldtrip12"},
    {"app": "WhatsApp", "sender": "3rd grade parents", "text": "Jen: Reminder: field trip form due today!"},
    {"app": "Remind", "sender": "Ms. Rivera · Room 12",
     "text": "Maya has a fever (101°F). Please pick her up by 1pm."},
]


class IngestBody(BaseModel):
    app: str = Field(min_length=1, max_length=60)
    sender: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=4000)


class ApproveBody(BaseModel):
    reply_text: str | None = Field(default=None, max_length=300)


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
        if not engine.resume(item_id, True, "page", reply_text=body.reply_text if body else None):
            return JSONResponse({"ok": False, "detail": "This message is not waiting for you any more."}, 409)
        return {"ok": True}

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

    @app.post("/api/demo/{index}")
    def demo(index: int, request: Request):
        require(request)
        if not settings.demo_mode:
            raise HTTPException(status_code=404, detail="Demo mode is off (set HUSHBELL_DEMO=1).")
        if not 0 <= index < len(DEMO_MESSAGES):
            raise HTTPException(status_code=404, detail="No such demo message.")
        msg = DEMO_MESSAGES[index]
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
        if item and digit in ("1", "2"):
            accepted = engine.resume(item_id, digit == "1", "call")
        return Response(gather_twiml(digit, item, settings.public_base_url, accepted), media_type="application/xml")

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
