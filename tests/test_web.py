from conftest import FakeVoice
from fastapi.testclient import TestClient

from hushbell.web.app import create_app

FEVER = {"app": "Remind", "sender": "Ms. Rivera · Room 12",
         "text": "Maya has a fever (101°F). Please pick her up by 1pm."}
AUTH = {"Authorization": "Bearer test-token"}


def test_page_needs_the_token(make_engine):
    client = TestClient(create_app(make_engine()))
    assert "Access token" in client.get("/").text
    assert client.get("/api/state").status_code == 401
    assert client.post("/login", data={"token": "wrong"}).status_code == 401
    r = client.post("/login", data={"token": "test-token"}, follow_redirects=False)
    assert r.status_code == 303 and "hushbell_session" in r.cookies
    assert client.get("/api/state").status_code == 200
    assert "Needs you" in client.get("/").text


def test_ingest_and_approve_from_the_page(make_engine):
    engine = make_engine()
    client = TestClient(create_app(engine))
    r = client.post("/api/ingest", json={"app": "Remind", "sender": "Ms. Rivera",
                                         "text": "Field trip form due TODAY: https://forms.gle/x"}, headers=AUTH)
    item_id = r.json()["item_id"]
    assert client.post(f"/api/items/{item_id}/approve", headers=AUTH).json() == {"ok": True}
    assert client.post(f"/api/items/{item_id}/approve", headers=AUTH).status_code == 409
    assert engine.store.get_item(item_id).status == "done"


def test_the_call_reads_the_summary_and_takes_a_keypress(make_engine):
    engine = make_engine(FakeVoice())
    client = TestClient(create_app(engine))
    item_id = client.post("/api/ingest", json=FEVER, headers=AUTH).json()["item_id"]

    twiml = client.post(f"/twilio/voice/{item_id}", data={"CallSid": "CA1"}).text
    assert "<Gather" in twiml and "Press 1 and I will reply to Ms. Rivera" in twiml and "on my way" in twiml

    reply = client.post(f"/twilio/gather/{item_id}", data={"Digits": "1"}).text
    assert "I am on it: I will reply to Ms. Rivera" in reply
    assert engine.store.get_item(item_id).status == "done"


def test_forms_cannot_be_approved_by_keypress(make_engine):
    """A link can't be checked by ear, so pressing 1 on a call never opens one."""
    engine = make_engine(FakeVoice())
    client = TestClient(create_app(engine))
    item_id = client.post("/api/ingest", json=FEVER, headers=AUTH).json()["item_id"]
    engine.store.update_item(item_id, action_type="open_link", url="https://forms.gle/x", reply_text="")

    assert "Open your Hushbell page" in client.post(f"/twilio/voice/{item_id}", data={}).text
    heard = client.post(f"/twilio/gather/{item_id}", data={"Digits": "1"}).text
    assert "approved on your Hushbell page" in heard
    assert engine.store.get_item(item_id).status == "calling" and engine.phone.goals == []

    engine.call_status(item_id, "completed")                 # the call ends; the card is still there
    assert engine.store.get_item(item_id).status == "waiting"
    assert client.post(f"/api/items/{item_id}/approve", headers=AUTH).json() == {"ok": True}
    assert "https://forms.gle/x" in engine.phone.goals[0]


def test_twilio_requests_must_be_signed(make_engine, settings):
    settings.twilio_validate = True
    settings.twilio_auth_token = "secret"
    client = TestClient(create_app(make_engine()))
    assert client.post("/twilio/voice/abc", data={"CallSid": "CA1"}).status_code == 403


def test_approve_with_an_edited_reply(make_engine):
    engine = make_engine(FakeVoice())
    client = TestClient(create_app(engine))
    item_id = client.post("/api/ingest", json=FEVER, headers=AUTH).json()["item_id"]
    r = client.post(f"/api/items/{item_id}/approve", json={"reply_text": "On my way, 20 minutes."}, headers=AUTH)
    assert r.json() == {"ok": True}
    assert engine.store.get_item(item_id).reply_text == "On my way, 20 minutes."


def test_retry_endpoint_refuses_when_nothing_failed(make_engine):
    client = TestClient(create_app(make_engine()))
    bake = {"app": "Remind", "sender": "t", "text": "Bake sale Friday! Bring cookies."}
    item_id = client.post("/api/ingest", json=bake, headers=AUTH).json()["item_id"]
    assert client.post(f"/api/items/{item_id}/retry", headers=AUTH).status_code == 409


def test_test_call(make_engine):
    voice = FakeVoice()
    client = TestClient(create_app(make_engine(voice)))
    assert client.post("/api/test-call", headers=AUTH).json() == {"ok": True}
    assert voice.calls == ["test"]
    assert "test call from Hushbell" in client.post("/twilio/test", data={}).text
    no_calls = TestClient(create_app(make_engine(None)))
    assert no_calls.post("/api/test-call", headers=AUTH).status_code == 409


def test_sign_in_locks_after_too_many_wrong_tokens(make_engine):
    client = TestClient(create_app(make_engine()))
    for _ in range(8):
        assert client.post("/login", data={"token": "wrong"}).status_code == 401
    assert client.post("/login", data={"token": "wrong"}).status_code == 429
    assert client.post("/login", data={"token": "test-token"}).status_code == 429   # locked for now


def test_demo_buttons_only_in_demo_mode(make_engine, settings):
    client = TestClient(create_app(make_engine()))
    assert client.post("/api/demo/0", headers=AUTH).status_code == 404
    settings.demo_mode = True
    client = TestClient(create_app(make_engine()))
    assert client.post("/api/demo/0", headers=AUTH).json()["ok"] is True


def test_ask_hushbell(make_engine):
    class FakeAssistant:
        def __init__(self):
            self.seen = None

        def answer(self, question, items):
            self.seen = (question, [i.text for i in items])
            return "Sign the field trip form today."

    engine = make_engine()
    client = TestClient(create_app(engine))
    assert "needs a Claude key" in client.post("/api/ask", json={"question": "What's due?"}, headers=AUTH).json()["answer"]
    engine.assistant = FakeAssistant()
    client.post("/api/ingest", json={"app": "Remind", "sender": "t", "text": "Field trip form due TODAY"}, headers=AUTH)
    r = client.post("/api/ask", json={"question": "What's due?"}, headers=AUTH)
    assert r.json() == {"answer": "Sign the field trip form today.", "searches": []}
    assert engine.assistant.seen == ("What's due?", ["Field trip form due TODAY"])
    assert client.post("/api/ask", json={"question": ""}, headers=AUTH).status_code == 422
    assert TestClient(create_app(engine)).post("/api/ask", json={"question": "x"}).status_code == 401


def test_assistant_lists_messages_for_claude():
    from hushbell.assistant import _message_lines
    from hushbell.models import Item

    item = Item(id="a", created_at=1794500000, updated_at=0, app="Remind", sender="Ms. Rivera",
                text="Picture day\nnext Thursday", status="later", category="later",
                event_date="2026-11-19", event_time="")
    line = _message_lines([item])
    assert "Remind / Ms. Rivera: Picture day next Thursday" in line
    assert "saved for later" in line and "about: 2026-11-19" in line


def test_demo_pickup_time_is_always_ahead():
    from datetime import datetime

    from hushbell.web.app import demo_pickup_time

    assert demo_pickup_time(datetime(2026, 11, 13, 10, 42)) == "12:30pm"
    assert demo_pickup_time(datetime(2026, 11, 13, 10, 30)) == "12pm"
    assert demo_pickup_time(datetime(2026, 11, 13, 18, 5)) == "8pm"


def test_demo_messages_are_about_the_familys_own_child(settings):
    from hushbell.web.app import demo_messages

    assert "Anna has a fever" in demo_messages(["anna"])[-1]["text"]
    assert "Maya has a fever" in demo_messages([])[-1]["text"]


def test_start_over_clears_everything_for_a_second_rehearsal(make_engine, settings):
    settings.demo_mode = True
    engine = make_engine()
    client = TestClient(create_app(engine))
    first = client.post("/api/demo/1", headers=AUTH).json()
    assert client.post("/api/demo/1", headers=AUTH).json()["new"] is False      # a repeat joins the first
    assert client.post("/api/demo/reset", headers=AUTH).json() == {"ok": True}
    assert engine.state()["items"] == []
    again = client.post("/api/demo/1", headers=AUTH).json()
    assert again["new"] is True and again["item_id"] != first["item_id"]        # fresh after starting over


def test_start_over_is_only_available_in_demo_mode(make_engine):
    client = TestClient(create_app(make_engine()))
    assert client.post("/api/demo/reset", headers=AUTH).status_code == 404
