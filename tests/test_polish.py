"""The final pass before the demo: wording, the rehearsal's simulated call, and messages that must not get lost."""

import time
from datetime import datetime

from conftest import FakeVoice
from fastapi.testclient import TestClient

import hushbell.web.app as web
from hushbell.config import Settings
from hushbell.models import Decision, IncomingMessage, Item
from hushbell.phone import ActionResult, DryRunPhone, UnsafeAction
from hushbell.plan import build_steps
from hushbell.voice import SimulatedVoice, for_voice, make_voice
from hushbell.web.app import create_app

AUTH = {"Authorization": "Bearer test-token"}
NURSE = "+14155550123"
FEVER = IncomingMessage(app="Remind", sender="Ms. Rivera · Room 12",
                        text="Maya has a fever (101°F). Please pick her up.")


def _wait(check, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- wording

def test_the_log_talks_to_the_parent_and_says_times_plainly(make_engine):
    engine = make_engine()
    later, _ = engine.ingest(IncomingMessage(app="Remind", sender="Ms. Rivera", text="Bake sale Friday!"))
    form, _ = engine.ingest(IncomingMessage(app="Remind", sender="Ms. Rivera", text="Trip form due today"))
    engine.resume(form.id, True, "page")
    details = [e["detail"] for e in engine.store.list_events()]
    assert "Saved for the 6 PM digest" in details                       # not "18:00", like the big screen
    assert "You approved on the Hushbell page" in details               # "You", like the rest of the log
    assert engine.state()["counts"]["taps_today"] == 1


def test_a_dry_run_sentence_ends_once(settings):
    done = DryRunPhone(settings).perform_step(
        Item(id="x", created_at=0, updated_at=0, app="Remind", sender="T", text="t"),
        {"id": "partner", "type": "text_partner", "to": "+14155550199", "name": "Sam", "text": "I'm on my way."})
    assert done.detail == "Dry run: would text Sam: “I'm on my way.”"


def test_names_typed_in_lowercase_are_shown_with_a_capital(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HUSHBELL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HUSHBELL_PARENT_NAME", "eli")
    monkeypatch.setenv("HUSHBELL_CHILDREN", "anna, Leo, mcKay")
    monkeypatch.setenv("HUSHBELL_PARTNER_NAME", "sam")
    settings = Settings.from_env()
    assert settings.parent_name == "Eli" and settings.partner_name == "Sam"
    assert settings.children == ["Anna", "Leo", "mcKay"]


def test_spoken_answers_never_read_a_web_address_aloud():
    said = for_voice("- **Sign** the form: https://forms.gle/x.\n- Hours (https://museum.example/hours)")
    assert said == "Sign the form: the link on your Hushbell page. Hours (the link on your Hushbell page)."


def test_the_sign_in_code_can_be_sized_by_the_page(make_engine):
    client = TestClient(create_app(make_engine()))
    assert "viewBox" in client.post("/api/pair", headers=AUTH).json()["svg"].split(">")[0]


# ---------------------------------------------------------------- when steps fail

class BrokenPhone:
    name = "broken"

    def perform(self, item):
        raise UnsafeAction("Twilio isn't set up.")

    def perform_step(self, item, step):
        raise UnsafeAction("Twilio isn't set up.")


def test_the_same_reason_is_said_once_and_names_the_right_phone(make_engine, settings):
    settings.partner_name, settings.partner_phone = "Sam", "+14155550199"
    engine = make_engine()
    engine.phone = BrokenPhone()
    item, _ = engine.ingest(FEVER)
    engine.resume(item.id, True, "page")
    failed = engine.store.get_item(item.id)
    assert failed.status == "failed"
    assert failed.result == "Stopped: Twilio isn't set up. Check the phone, then press Try again."

    settings.phone_backend = "phoneuse"
    engine.retry(item.id)
    assert engine.store.get_item(item.id).result.endswith("Check the cloud phone, then press Try again.")
    assert any(e["detail"].startswith("Working on the cloud phone") for e in engine.store.list_events())


def test_a_reason_that_already_says_what_to_do_is_left_alone(make_engine):
    class NeedsPerson(DryRunPhone):
        def perform(self, item):
            return ActionResult(False, "Finish it in the PhoneUse console, then press Try again.")

    engine = make_engine()
    engine.phone = NeedsPerson(engine.settings)
    item, _ = engine.ingest(IncomingMessage(app="Remind", sender="Ms. Rivera", text="Trip form due today"))
    engine.resume(item.id, True, "page")
    assert engine.store.get_item(item.id).result.count("Try again") == 1


def test_a_cloud_phone_calendar_step_stays_a_link_to_tap(make_engine):
    class LinkPhone(DryRunPhone):
        def perform_step(self, item, step):
            return ActionResult(True, "Tap to add it to your calendar.", link="https://calendar.google.com/x")

    class Dated:
        def classify(self, app, sender, text, also_seen_in=None):
            return Decision(category="later", reason="Next week", summary="Picture day is next week.",
                            date=time.strftime("%Y-%m-%d"), event_title="Picture day")

    engine = make_engine()
    engine.phone, engine.classifier = LinkPhone(engine.settings), Dated()
    item, _ = engine.ingest(IncomingMessage(app="Gmail", sender="Lincoln Elementary", text="Picture day soon"))
    assert engine.coming_up()[0]["link"] == ""
    assert engine.run_step_now(item.id, "calendar")
    assert engine.coming_up()[0]["link"] == "https://calendar.google.com/x"


def test_in_cloud_mode_the_time_to_leave_step_is_called_a_call(settings):
    settings.children, settings.phone_backend = ["Anna"], "phoneuse"
    decision = Decision(category="urgent", reason="Sick", summary="Anna needs pickup by 1 pm.", child="Anna",
                        action_type="reply", reply_text="On my way.", date="2026-11-12", time="13:00")
    item = Item(id="x", created_at=0, updated_at=0, app="Remind", sender="Ms. Rivera", text="Anna has a fever")
    alarm = build_steps(item, decision, settings, ["fever"], now=datetime(2026, 11, 12, 10, 42))[1]
    assert alarm["type"] == "set_alarm" and alarm["label"] == "Call you at 12:30 PM when it's time to leave"


# ---------------------------------------------------------------- a caller's message is never lost

class SayLater:
    def classify(self, app, sender, text, also_seen_in=None):
        return Decision(category="later", reason="General", summary=f"Caller said: {text[:60]}")


def _line(make_engine, voice=None):
    engine = make_engine(voice)
    engine.classifier = SayLater()
    return engine, TestClient(create_app(engine))


def test_a_caller_who_hangs_up_still_gets_their_message_through(make_engine):
    engine, client = _line(make_engine)
    said = "Hi, it's Coach Diaz, practice moved to field B on Saturday."
    client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA1", "SpeechResult": said})
    assert engine.state()["items"] == []
    assert client.post("/twilio/inbound/ended", data={"CallSid": "CA1", "CallStatus": "completed"}).status_code == 204
    items = engine.state()["items"]
    assert len(items) == 1 and items[0]["text"] == said and items[0]["category"] == "later"
    # A late keypress or a second notice for the same call adds nothing.
    client.post("/twilio/inbound/emergency?none=1", data={"From": NURSE, "CallSid": "CA1"})
    client.post("/twilio/inbound/ended", data={"CallSid": "CA1"})
    assert len(engine.state()["items"]) == 1


def test_a_message_is_passed_on_even_if_twilio_says_nothing_more(make_engine, monkeypatch):
    monkeypatch.setattr(web, "PENDING_CALL_SECONDS", 0.05)
    engine, client = _line(make_engine)
    client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA2", "SpeechResult": "The book fair is Monday."})
    assert _wait(lambda: len(engine.state()["items"]) == 1)


def test_a_callers_number_is_written_the_way_people_write_it(make_engine):
    engine, client = _line(make_engine)
    client.post("/twilio/inbound/heard", data={"From": NURSE, "CallSid": "CA3",
                                               "SpeechResult": "Maya has a fever, please pick her up."})
    assert any(e["detail"].startswith("Phone call · (415) 555-0123: ") for e in engine.store.list_events())


# ---------------------------------------------------------------- the rehearsal's simulated call

def test_a_simulated_voice_only_exists_for_a_demo_without_twilio(settings):
    settings.simulate_calls = True
    assert make_voice(settings) is None                                  # not a demo run
    settings.demo_mode, settings.simulate_calls = True, False
    assert make_voice(settings) is None                                  # a plain demo keeps "calls are off"
    settings.simulate_calls = True
    assert isinstance(make_voice(settings), SimulatedVoice)
    settings.twilio_account_sid = settings.twilio_auth_token = "x"
    settings.twilio_from_number, settings.parent_phone = "+15550000000", "+15550000001"
    assert not isinstance(make_voice(settings), SimulatedVoice)          # with Twilio, calls are real


def test_simulated_calls_are_refused_outside_a_rehearsal(make_engine, settings):
    settings.demo_mode = True
    client = TestClient(create_app(make_engine(FakeVoice())))            # a real voice: never simulated
    assert client.post("/api/demo/call", headers=AUTH).status_code == 404
    assert client.post("/api/demo/press/1", headers=AUTH).status_code == 404
    assert client.get("/api/state", headers=AUTH).json()["setup"]["calls_simulated"] is False
    settings.demo_mode = False
    client = TestClient(create_app(make_engine(SimulatedVoice())))       # not a demo run
    assert client.post("/api/demo/call", headers=AUTH).status_code == 404
    assert client.post("/api/demo/call").status_code == 401


def test_rehearsing_the_nurses_call_and_pressing_1(make_engine, settings, monkeypatch):
    monkeypatch.setattr(web, "DEMO_CALL_SECONDS", 0.05)
    settings.demo_mode, settings.parent_name = True, "Eli"
    engine, client = _line(make_engine, SimulatedVoice())
    assert client.get("/api/state", headers=AUTH).json()["setup"]["calls_simulated"] is True
    assert client.post("/api/demo/press/1", headers=AUTH).status_code == 409          # nothing is ringing yet

    assert client.post("/api/demo/call", headers=AUTH).json() == {"ok": True}
    assert engine.store.list_events()[0]["detail"] == "Hushbell answered a call from (415) 555-0134 (simulated)"
    assert _wait(lambda: any(i["status"] == "calling" for i in engine.state()["items"]))
    item = engine.state()["items"][0]
    assert item["app"] == "Phone call" and item["category"] == "urgent" and item["call_attempts"] == 1
    assert "Rehearsal: a simulated call. No phone is ringing" in [e["detail"] for e in engine.store.list_events()]

    assert client.post("/api/demo/press/1", headers=AUTH).json() == {"ok": True, "item_id": item["id"]}
    done = engine.store.get_item(item["id"])
    assert done.status == "done"
    assert done.steps[0]["detail"] == "Rehearsal: on a real call you would be connected to them now."
    assert client.post("/api/demo/press/1", headers=AUTH).status_code == 409


def test_the_call_reads_a_reply_without_a_doubled_full_stop():
    from hushbell.voice import call_script

    steps = [{"id": "main", "type": "reply", "say": "reply to Ms. Rivera", "enabled": True, "status": "pending"}]
    item = Item(id="x", created_at=0, updated_at=0, app="Remind", sender="Ms. Rivera", text="t", summary="Maya is sick.",
                action_type="reply", reply_text="I'm on my way.", steps=steps)
    assert "Your reply will say: I'm on my way. Press 2" in call_script(item)
    item.reply_text = "On my way"
    assert "Your reply will say: On my way. Press 2" in call_script(item)


# ---------------------------------------------------------------- the command line says what to do next

def test_send_tells_a_wrong_token_from_a_stopped_hushbell(settings, monkeypatch, capsys):
    import io
    import urllib.error
    from types import SimpleNamespace

    from hushbell import cli

    args = SimpleNamespace(app="Remind", sender="Ms. Rivera", text="Bake sale Friday")

    def refuse(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(cli.urllib.request, "urlopen", refuse)
    assert cli.cmd_send(settings, args) == 1
    assert "didn't accept the access token" in capsys.readouterr().out

    def nobody(req, timeout=0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cli.urllib.request, "urlopen", nobody)
    assert cli.cmd_send(settings, args) == 1
    assert "Is `hushbell run` going?" in capsys.readouterr().out

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cli.urllib.request, "urlopen",
                        lambda req, timeout=0: Answer(b'{"ok": true, "item_id": "abc", "new": false}'))
    assert cli.cmd_send(settings, args) == 0
    assert "joined them to the earlier message" in capsys.readouterr().out


def test_notifications_says_how_to_get_adb(settings, capsys):
    from hushbell import cli

    settings.adb_path = "adb-that-is-not-installed"
    assert cli.cmd_notifications(settings) == 1
    assert "Install Android platform-tools" in capsys.readouterr().out
    settings.phone_backend = "phoneuse"
    assert cli.cmd_notifications(settings) == 1
    assert "hushbell doctor --live" in capsys.readouterr().out


def test_init_stops_cleanly_when_nobody_can_answer(tmp_path, monkeypatch, capsys):
    import io

    from hushbell import cli

    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-api03-EXAMPLEKEY\nanna\n"))
    target = tmp_path / "new-folder" / ".env"
    assert cli.main(["init", "--file", str(target)]) == 1
    assert "Every answer so far is saved" in capsys.readouterr().out
    assert "HUSHBELL_CHILDREN=anna" in target.read_text(encoding="utf-8")
