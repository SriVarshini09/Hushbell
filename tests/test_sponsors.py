"""The partner tools: PhoneUse cloud phone, ElevenLabs voice, Tavily search, LangSmith, Replit, and the app install."""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from hushbell.assistant import Assistant
from hushbell.models import IncomingMessage, Item
from hushbell.narrator import Narrator
from hushbell.phone import GUARD, ActionResult, UnsafeAction
from hushbell.phoneuse import (PhoneUseClient, PhoneUseNotificationSource, PhoneUsePhone, ShadeReading,
                               calendar_link, maps_link, next_time)
from hushbell.tracing import traced
from hushbell.tunnel import detect_replit
from hushbell.web.app import create_app

AUTH = {"Authorization": "Bearer test-token"}


# ---------------------------------------------------------------- a fake PhoneUse service

class FakePhoneUse:
    """Answers like https://mcp.phoneuse.com, and records what Hushbell asked for."""

    def __init__(self, run_states=("running", "succeeded"), lease_taken=False):
        self.requests: list[httpx.Request] = []
        self.run_states = list(run_states)
        self.lease_taken = lease_taken
        self.screen = b"\xff\xd8shade-1"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/mcp":
            call = json.loads(request.content)["params"]
            if call["name"] == "list_devices":
                devices = [{"deviceId": "pixel-offline", "kind": "cloud_phone", "status": "offline"},
                           {"deviceId": "cloud-7", "kind": "cloud_phone", "status": "online"}]
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": {"structuredContent": {"devices": devices}}})
            if call["name"] == "wake":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                                 "result": {"structuredContent": {"accepted": True}}})
        if path.endswith("/screenshot"):
            return httpx.Response(200, content=self.screen, headers={"content-type": "image/jpeg"})
        if path.endswith("/lease") and method == "POST":
            if self.lease_taken:
                return httpx.Response(409, json={"error": {"code": "device_unavailable", "message": "leased",
                                                           "retryable": False}})
            return httpx.Response(201, json={"leaseId": "lease-1"})
        if path.endswith("/lease") and method == "PATCH":
            return httpx.Response(200, json={"leaseId": "lease-held"})
        if path.endswith("/lease") and method == "DELETE":
            return httpx.Response(204)
        if path.endswith("/actions"):
            return httpx.Response(200, json={"status": "committed"})
        if path == "/v1/runs" and method == "POST":
            return httpx.Response(201, json={"runId": "task_1", "state": "queued", "executor": "phoneuse"})
        if path == "/v1/runs/task_1" and method == "GET":
            state = self.run_states.pop(0) if self.run_states else "succeeded"
            body = {"runId": "task_1", "state": state}
            if state == "succeeded":
                body["detail"] = "Reply sent in Remind."
            if state == "failed":
                body["reason"] = "budget_exhausted"
            return httpx.Response(200, json=body)
        if path.endswith("/cancel"):
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})

    def bodies(self, path_end: str, method: str = "POST") -> list[dict]:
        return [json.loads(r.content or b"{}") for r in self.requests
                if r.url.path.endswith(path_end) and r.method == method]


def _client(fake: FakePhoneUse) -> PhoneUseClient:
    return PhoneUseClient("pbk_test_secret", http=httpx.Client(transport=httpx.MockTransport(fake)),
                          sleep=lambda s: None)


def _item(**kw) -> Item:
    base = dict(id="i1", created_at=0, updated_at=0, app="Remind", sender="Ms. Rivera · Room 12",
                text="Can Maya stay late?", action_type="reply", reply_text="Yes, she can stay.")
    base.update(kw)
    return Item(**base)


def test_phoneuse_runs_a_reply_as_a_task_on_the_cloud_phone(settings):
    fake = FakePhoneUse()
    phone = PhoneUsePhone(settings, client=_client(fake))
    result = phone.perform(_item())
    assert result.ok and result.detail == "Reply sent in Remind."
    run = fake.bodies("/v1/runs")[0]
    assert run["deviceId"] == "cloud-7" and run["executor"] == "phoneuse" and run["leaseId"] == "lease-1"
    assert 'Type exactly this reply and send it: "Yes, she can stay."' in run["goal"] and GUARD in run["goal"]
    assert fake.bodies("/lease", "DELETE") == [{"leaseId": "lease-1"}]          # the phone is given back
    assert all("origin" not in r.headers for r in fake.requests)               # API keys never send Origin
    assert all(r.headers["authorization"] == "Bearer pbk_test_secret" for r in fake.requests)


def test_a_task_that_needs_a_person_keeps_the_phone_for_them(settings):
    fake = FakePhoneUse(run_states=["running", "needs_user_control"])
    result = PhoneUsePhone(settings, client=_client(fake)).perform(_item())
    assert not result.ok and "PhoneUse console" in result.detail
    assert fake.bodies("/lease", "DELETE") == []


def test_a_failed_task_says_why_and_an_held_phone_is_reused(settings):
    fake = FakePhoneUse(run_states=["failed"], lease_taken=True)
    result = PhoneUsePhone(settings, client=_client(fake)).perform(_item())
    assert not result.ok and "budget exhausted" in result.detail
    assert fake.bodies("/v1/runs")[0]["leaseId"] == "lease-held"


class FakeMessenger:
    def __init__(self):
        self.texts, self.reminders = [], []

    def send_text(self, to, body):
        self.texts.append((to, body))

    def call_later(self, at, words):
        self.reminders.append((at, words))


def test_cloud_phone_steps_reach_the_parents_real_phone(settings):
    messenger = FakeMessenger()
    phone = PhoneUsePhone(settings, client=_client(FakePhoneUse()), messenger=messenger)
    item = _item()
    texted = phone.perform_step(item, {"id": "partner", "type": "text_partner", "to": "+14155550199",
                                       "name": "Sam", "text": "Heads up: I'm on my way."})
    assert texted.ok and messenger.texts == [("+14155550199", "Ana Lopez (via Hushbell): Heads up: I'm on my way.")]
    alarm = phone.perform_step(item, {"id": "alarm", "type": "set_alarm", "time": "13:30", "message": "Leave now"})
    assert alarm.ok and "1:30 PM" in alarm.detail and messenger.reminders[0][0].strftime("%H:%M") == "13:30"
    directions = phone.perform_step(item, {"id": "d", "type": "directions", "destination": "123 Main St"})
    assert directions.link == maps_link("123 Main St") and "destination=123+Main+St" in directions.link
    calendar = phone.perform_step(item, {"id": "c", "type": "add_calendar", "title": "Field trip",
                                         "date": "2026-11-20", "time": ""})
    assert "calendar.google.com" in calendar.link and "dates=20261120%2F20261121" in calendar.link
    back = phone.perform_step(item, {"id": "m", "type": "call_back", "number": "+14155550123"})
    assert back.link == "tel:+14155550123"
    with pytest.raises(UnsafeAction):
        PhoneUsePhone(settings, client=_client(FakePhoneUse())).perform_step(
            item, {"id": "partner", "type": "text_partner", "to": "+14155550199", "name": "Sam", "text": "hi"})


def test_links_and_times():
    assert "dates=20261120T143000%2F20261120T153000" in calendar_link(
        {"title": "Chess", "date": "2026-11-20", "time": "14:30"}, "note")
    evening = datetime(2026, 11, 12, 23, 0)
    assert next_time("01:00", evening) == datetime(2026, 11, 13, 1, 0)
    assert next_time("23:30", evening) == datetime(2026, 11, 12, 23, 30)


class FakeReader:
    def __init__(self, readings):
        self.readings = list(readings)
        self.calls = 0

    def read(self, image, media_type="image/jpeg"):
        self.calls += 1
        return self.readings.pop(0)


def test_reading_the_cloud_phone_notification_shade(settings):
    settings.backfill = True
    fake = FakePhoneUse()
    reader = FakeReader([
        ShadeReading(True, [
            {"app": "Remind", "title": "Ms. Rivera · Room 12", "text": "Field trip form due today"},
            {"app": "WhatsApp", "title": "Mom", "text": "Dinner at 7?"},                      # personal: skipped
            {"app": "WhatsApp", "title": "3rd grade parents", "text": "Jen: form due today!"},
            {"app": "Android System", "title": "USB debugging", "text": "connected"},
        ]),
        ShadeReading(False, []),
    ])
    source = PhoneUseNotificationSource(settings, _client(fake), reader)
    first = source.poll()
    assert [(m.app, m.sender) for m in first] == [("Remind", "Ms. Rivera · Room 12"), ("WhatsApp", "3rd grade parents")]
    swipe = fake.bodies("/actions")[0]
    assert swipe["action"] == "swipe" and swipe["fromY"] < swipe["toY"]          # pulled the shade down
    assert source.poll() == [] and reader.calls == 1                             # same picture: no second read
    fake.screen = b"\xff\xd8locked"
    assert source.poll() == [] and source.client.shade_open is False             # closed: reopen next time


def test_reading_waits_while_a_task_runs(settings):
    client = _client(FakePhoneUse())
    source = PhoneUseNotificationSource(settings, client, FakeReader([]))
    with client.busy:
        assert source.poll() == []


# ---------------------------------------------------------------- ElevenLabs

def _narrator(settings, handler):
    settings.elevenlabs_api_key = "xi-test"
    return Narrator(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_calls_speak_in_the_elevenlabs_voice(settings):
    asked = []

    def handler(request):
        asked.append(json.loads(request.content))
        assert request.headers["xi-api-key"] == "xi-test" and request.url.params["output_format"] == "mp3_22050_32"
        return httpx.Response(200, content=b"ID3fake-mp3")

    narrator = _narrator(settings, handler)
    twiml = "<Response><Gather><Say>Hi, it&apos;s Hushbell. Press 1.</Say></Gather><Say>Goodbye.</Say></Response>"
    out = narrator.narrate(twiml)
    assert "<Say>" not in out and out.count("<Play>https://hushbell.example/voice/") == 2
    assert {a["text"] for a in asked} == {"Hi, it's Hushbell. Press 1.", "Goodbye."}
    narrator.narrate(twiml)
    assert len(asked) == 2                                                        # clips are made once
    clip = out.split("/voice/")[1].split(".mp3")[0]
    assert narrator.path(clip).read_bytes() == b"ID3fake-mp3" and narrator.path("../../etc") is None


def test_twilio_voice_is_kept_when_elevenlabs_fails(settings):
    narrator = _narrator(settings, lambda r: httpx.Response(401, text="bad key"))
    twiml = "<Response><Say>Goodbye.</Say></Response>"
    assert narrator.narrate(twiml) == twiml
    settings.elevenlabs_api_key = ""
    assert not Narrator(settings).enabled


def test_twilio_fetches_the_voice_clips(make_engine, settings):
    settings.elevenlabs_api_key = "xi-test"
    engine = make_engine()
    narrator = Narrator(settings)
    (settings.data_dir / "voice").mkdir()
    clip = narrator.clip_id("Goodbye.")
    (settings.data_dir / "voice" / f"{clip}.mp3").write_bytes(b"ID3")
    client = TestClient(create_app(engine))
    got = client.get(f"/voice/{clip}.mp3")
    assert got.status_code == 200 and got.headers["content-type"] == "audio/mpeg"
    assert client.get("/voice/" + "0" * 32 + ".mp3").status_code == 404


# ---------------------------------------------------------------- Tavily

class FakeClaude:
    """First asks for a web search, then answers from it."""

    def __init__(self):
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) == 1:
            block = SimpleNamespace(type="tool_use", id="tu1", name="web_search",
                                    input={"query": "Exploratorium hours"})
            return SimpleNamespace(stop_reason="tool_use", content=[block])
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text="Open 10 to 5 on Friday (from Exploratorium)")])


def test_ask_hushbell_can_look_things_up_with_tavily(make_engine, settings):
    claude, searched = FakeClaude(), []

    def search(query):
        searched.append(query)
        return "Summary: open 10am to 5pm"

    engine = make_engine()
    engine.assistant = Assistant(settings, client=claude, search=search)
    answer = engine.ask("When does the museum open on the field trip day?")
    assert answer.startswith("Open 10 to 5") and searched == ["Exploratorium hours"]
    assert claude.requests[0]["tools"][0]["name"] == "web_search"
    tool_result = claude.requests[1]["messages"][-1]["content"][0]
    assert tool_result["type"] == "tool_result" and "<web_results>" in tool_result["content"]
    assert any(e["kind"] == "web" and "Exploratorium hours" in e["detail"] for e in engine.store.list_events())


def test_without_a_tavily_key_ask_has_no_web_tool(settings):
    claude = FakeClaude()
    claude.requests.append({})       # skip the tool round: answer straight away
    assert Assistant(settings, client=claude).answer("Anything due?", []).startswith("Open 10")
    assert "tools" not in claude.requests[1]


def test_tavily_results_are_boiled_down():
    from hushbell.assistant import web_search

    class Tavily:
        def search(self, query, max_results, include_answer):
            return {"answer": "Open 10-5.", "results": [{"title": "Hours", "url": "https://x.org", "content": "10am"}]}

    assert web_search("hours", "k", client=Tavily()) == "Summary: Open 10-5.\n- Hours (https://x.org): 10am"


# ---------------------------------------------------------------- LangSmith, Replit

def test_tracing_is_a_plain_call_when_off(monkeypatch):
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert traced("x", lambda a, b=0: a + b, 2, b=3) == 5


def test_replit_address_is_found():
    assert detect_replit({"REPLIT_DOMAINS": "hushbell.replit.app,other"}) == "https://hushbell.replit.app"
    assert detect_replit({"REPLIT_DEV_DOMAIN": "abc.picard.replit.dev"}) == "https://abc.picard.replit.dev"
    assert detect_replit({}) == ""


# ---------------------------------------------------------------- the installable app

def test_hushbell_installs_like_an_app(make_engine):
    client = TestClient(create_app(make_engine()))
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    data = manifest.json()
    assert data["display"] == "standalone" and {i["sizes"] for i in data["icons"]} >= {"192x192", "512x512"}
    assert client.get("/sw.js").headers["service-worker-allowed"] == "/"
    assert client.get("/icons/icon-192.png").content[:4] == b"\x89PNG"
    assert client.get("/apple-touch-icon.png").status_code == 200
    assert client.get("/icons/../app.py").status_code == 404
    assert "offline" in client.get("/offline.html").text


def test_scan_to_sign_in_works_once(make_engine):
    client = TestClient(create_app(make_engine()))
    assert client.post("/api/pair").status_code == 401
    made = client.post("/api/pair", headers=AUTH).json()
    assert made["url"].startswith("https://hushbell.example/pair/") and made["svg"].startswith("<svg")
    phone = TestClient(client.app)
    path = made["url"].replace("https://hushbell.example", "")
    first = phone.get(path, follow_redirects=False)
    assert first.status_code == 303 and "hushbell_session" in first.headers["set-cookie"]
    assert phone.get(path, follow_redirects=False).status_code == 410            # one use only


def test_a_step_link_is_kept_for_the_page(make_engine, settings):
    engine = make_engine()

    class LinkPhone:
        name = "links"

        def perform(self, item):
            return ActionResult(True, "Sent.")

        def perform_step(self, item, step):
            return ActionResult(True, "Directions are ready.", link="https://www.google.com/maps/dir/?api=1")

    engine.phone = LinkPhone()
    item, _ = engine.ingest(IncomingMessage(app="Remind", sender="Ms. Rivera", text="Maya has a fever, please pick her up"))
    step = {"id": "directions", "type": "directions", "destination": "1 Main St", "label": "Open directions"}
    engine.store.update_item(item.id, steps=[dict(step, enabled=True, status="pending", detail="")])
    ok, _ = engine._do_step(engine.store.get_item(item.id), step)
    assert ok and step["link"].startswith("https://www.google.com/maps")
