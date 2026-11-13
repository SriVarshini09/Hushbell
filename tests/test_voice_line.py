"""Calling Hushbell: the school (or a judge) calls and speaks; the parent calls to ask."""

from conftest import FakeVoice
from fastapi.testclient import TestClient

from hushbell.models import Decision
from hushbell.voice import configure_inbound, for_voice
from hushbell.web.app import create_app

NURSE = "+14155550123"
AUTH = {"Authorization": "Bearer test-token"}


class SayLater:
    def classify(self, app, sender, text, also_seen_in=None):
        return Decision(category="later", reason="General", summary=f"Caller said: {text[:60]}")


def _client(make_engine, settings, voice=None):
    settings.parent_phone = "+14155559999"
    settings.parent_name = "Eli"
    engine = make_engine(voice)
    engine.classifier = SayLater()
    return engine, TestClient(create_app(engine))


def test_hushbell_answers_and_asks_who_is_calling(make_engine, settings):
    _, client = _client(make_engine, settings)
    twiml = client.post("/twilio/inbound", data={"From": NURSE, "CallSid": "CA1"}).text
    assert 'input="speech"' in twiml and "assistant for Eli's family" in twiml
    assert "/twilio/inbound/heard" in twiml


def test_an_urgent_call_rings_the_parent_straight_away(make_engine, settings):
    voice = FakeVoice()
    engine, client = _client(make_engine, settings, voice)
    said = "Hi, this is the school nurse at Lincoln. Maya has a fever and needs to be picked up."
    heard = client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA1", "SpeechResult": said}).text
    assert "This sounds urgent, so I'm calling Eli right now" in heard
    item = engine.state()["items"][0]
    assert item["app"] == "Phone call" and item["sender"] == NURSE and item["category"] == "urgent"
    assert item["status"] == "calling" and voice.calls == [item["id"]]
    assert [s["type"] for s in item["steps"]][0] == "call_back"

    # The parent hears who called and presses 1: Twilio connects them to the nurse.
    script = client.post(f"/twilio/voice/{item['id']}", data={}).text
    assert "someone just called for you" in script and "Press 1 and I will connect you to them" in script
    connect = client.post(f"/twilio/gather/{item['id']}", data={"Digits": "1"}).text
    assert "Connecting you now" in connect and f"<Number>{NURSE}</Number>" in connect
    done = engine.store.get_item(item["id"])
    assert done.status == "done" and done.steps[0]["status"] == "done"


def test_a_calm_call_asks_whether_it_is_an_emergency(make_engine, settings):
    voice = FakeVoice()
    engine, client = _client(make_engine, settings, voice)
    said = "Hi, it's Coach Diaz, practice moved to field B on Saturday."
    ask = client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA2", "SpeechResult": said}).text
    assert "If this is an emergency, press 1 now" in ask
    assert engine.state()["items"] == []                              # nothing filed until they answer
    done = client.post("/twilio/inbound/emergency?none=1", data={"From": NURSE, "CallSid": "CA2"}).text
    assert "I've passed that on" in done
    item = engine.state()["items"][0]
    assert item["category"] == "later" and voice.calls == []


def test_the_caller_can_press_1_for_an_emergency(make_engine, settings):
    voice = FakeVoice()
    engine, client = _client(make_engine, settings, voice)
    said = "This is the front office, please call us back about your son."
    client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA3", "SpeechResult": said})
    done = client.post("/twilio/inbound/emergency", data={"From": NURSE, "CallSid": "CA3", "Digits": "1"}).text
    assert "calling Eli right now" in done
    item = engine.state()["items"][0]
    assert item["category"] == "urgent" and "emergency" in item["rule_hit"] and len(voice.calls) == 1


def test_silence_gets_one_more_try(make_engine, settings):
    _, client = _client(make_engine, settings)
    again = client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA4", "SpeechResult": ""}).text
    assert "didn't catch that" in again


def test_the_parent_calls_to_ask(make_engine, settings):
    engine, client = _client(make_engine, settings)

    class FakeAssistant:
        def answer(self, question, items):
            return "- Sign the field trip form today\n- Bake sale Friday"

    engine.assistant = FakeAssistant()
    hello = client.post("/twilio/inbound", data={"From": "(415) 555-9999", "CallSid": "CA5"}).text
    assert "What would you like to know?" in hello and "/twilio/ask" in hello
    answer = client.post("/twilio/ask", data={"From": "+14155559999", "SpeechResult": "What's due this week?"}).text
    assert "Sign the field trip form today. Bake sale Friday. Anything else?" in answer
    stranger = client.post("/twilio/ask", data={"From": NURSE, "SpeechResult": "What's due?"}).text
    assert "Sign the field trip" not in stranger                     # only the parent can ask


def test_voice_answers_read_cleanly():
    assert for_voice("- First thing\n- Second thing!\nThird") == "First thing. Second thing! Third."


def test_twilio_number_is_pointed_at_hushbell(settings):
    settings.twilio_account_sid, settings.twilio_auth_token = "AC1", "t"
    settings.twilio_from_number, settings.parent_phone = "+15550000000", "+15550000001"
    updates = []

    class Number:
        def update(self, **kw):
            updates.append(kw)

    class Numbers:
        def __init__(self, found):
            self.found = found

        def list(self, phone_number, limit):
            return [Number()] if self.found else []

    class Client:
        def __init__(self, found=True):
            self.incoming_phone_numbers = Numbers(found)

    assert configure_inbound(settings, client=Client()) == "Calls to +15550000000 now reach Hushbell."
    assert updates == [{"voice_url": "https://hushbell.example/twilio/inbound", "voice_method": "POST",
                        "status_callback": "https://hushbell.example/twilio/inbound/ended",
                        "status_callback_method": "POST"}]
    assert "Couldn't find" in configure_inbound(settings, client=Client(found=False))


def test_the_big_screen_gets_names_and_call_counts(make_engine, settings):
    voice = FakeVoice()
    engine, client = _client(make_engine, settings, voice)
    settings.partner_name = "Sam"
    said = "This is the school nurse. Maya has a fever and needs to be picked up."
    client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA9", "SpeechResult": said})
    state = client.get("/api/state", headers=AUTH).json()
    assert state["setup"]["parent_name"] == "Eli" and state["setup"]["partner_name"] == "Sam"
    assert state["items"][0]["call_attempts"] == 1 and state["now"] > 0


def test_presenter_view_needs_sign_in(make_engine):
    client = TestClient(create_app(make_engine()))
    assert "Access token" in client.get("/present").text
    client.post("/login", data={"token": "test-token"})
    assert "What Hushbell decided" in client.get("/present").text
