"""The plan for each kind of message, the Android shortcuts behind it, and running the steps."""

from datetime import datetime
from subprocess import CompletedProcess

from conftest import FakeVoice

from hushbell.models import Decision, IncomingMessage, Item
from hushbell.phone import MobilerunPhone
from hushbell.plan import build_steps, spoken_list
from hushbell.shortcuts import AndroidShortcuts, calendar_times
from hushbell.store import Store

NOW = datetime(2026, 11, 12, 10, 42)


def _item(text="Anna has a fever (101°F) and needs to be picked up by 1pm."):
    return Item(id="x", created_at=0, updated_at=0, app="Remind", sender="Ms. Rivera · Room 12", text=text)


def _family(settings):
    settings.children = ["Anna"]
    settings.schools = ["Lincoln Elementary"]
    settings.school_address = "123 Main St, San Francisco, CA"
    settings.partner_name = "Sam"
    settings.partner_phone = "+14155550199"
    settings.travel_minutes = 30
    return settings


FEVER = Decision(category="urgent", reason="Sick child", summary="Anna has a fever and needs pickup by 1 pm.",
                 child="Anna", action_type="reply", reply_text="On my way.", date="2026-11-12", time="13:00")


def test_urgent_pickup_gets_the_whole_plan(settings):
    steps = build_steps(_item(), FEVER, _family(settings), ["fever", "pickup now"], now=NOW)
    assert [s["type"] for s in steps] == ["reply", "set_alarm", "text_partner", "directions"]
    alarm = steps[1]
    assert alarm["time"] == "12:30" and "12:30 PM" in alarm["label"] and alarm["message"] == "Leave now to pick up Anna"
    assert steps[2]["text"] == "Heads up: school says Anna needs to be picked up by 1 PM. I'm on my way."
    assert steps[3]["destination"] == "123 Main St, San Francisco, CA"
    assert all(s["enabled"] and s["status"] == "pending" for s in steps)
    assert spoken_list(steps) == ("reply to Ms. Rivera, set an alarm for 12:30 PM, text Sam, "
                                  "and open directions to Lincoln Elementary")


def test_no_alarm_when_it_is_already_too_late_or_there_is_no_time(settings):
    late = datetime(2026, 11, 12, 12, 45)
    assert "set_alarm" not in [s["type"] for s in build_steps(_item(), FEVER, _family(settings), ["fever"], now=late)]
    no_time = FEVER.model_copy(update={"time": ""})
    assert "set_alarm" not in [s["type"] for s in build_steps(_item(), no_time, _family(settings), ["fever"], now=NOW)]


def test_a_pickup_after_midnight_still_gets_its_alarm(settings):
    evening = datetime(2026, 11, 12, 23, 50)
    after_midnight = FEVER.model_copy(update={"date": "", "time": "01:30"})
    steps = build_steps(_item(), after_midnight, _family(settings), ["fever"], now=evening)
    alarm = next(s for s in steps if s["type"] == "set_alarm")
    assert alarm["time"] == "01:00"
    next_week = FEVER.model_copy(update={"date": "2026-11-19"})
    assert "set_alarm" not in [s["type"] for s in build_steps(_item(), next_week, _family(settings), ["fever"], now=NOW)]


def test_extras_only_appear_when_set_up(settings):
    steps = build_steps(_item(), FEVER, settings, ["fever"], now=NOW)       # no partner, no address
    assert [s["type"] for s in steps] == ["reply", "set_alarm"]
    settings.partner_name, settings.partner_phone = "Sam", "call me maybe"  # not a phone number
    assert "text_partner" not in [s["type"] for s in build_steps(_item(), FEVER, settings, ["fever"], now=NOW)]


def test_lockdown_texts_the_other_parent_but_sets_no_pickup_alarm(settings):
    lockdown = Decision(category="urgent", reason="Lockdown", summary="Lincoln is in lockdown; do not come to school.",
                        action_type="reply", reply_text="Thank you.", time="11:00")
    steps = build_steps(_item("The school is in lockdown."), lockdown, _family(settings), ["lockdown"], now=NOW)
    assert [s["type"] for s in steps] == ["reply", "text_partner"]
    assert steps[1]["text"] == "Heads up from school: Lincoln is in lockdown; do not come to school."


def test_form_and_future_event_go_on_the_calendar(settings):
    form = Decision(category="needs_you", reason="Form due", summary="Field trip form due today.",
                    action_type="open_link", url="https://forms.gle/x", date="2026-11-20",
                    event_title="Anna: science museum trip")
    steps = build_steps(_item("Field trip next Friday. Form due today."), form, settings, [], now=NOW)
    assert [s["type"] for s in steps] == ["open_link", "add_calendar"]
    assert steps[1]["date"] == "2026-11-20" and "(Fri, Nov 20)" in steps[1]["label"]
    past = form.model_copy(update={"date": "2026-11-01"})
    assert [s["type"] for s in build_steps(_item(), past, settings, [], now=NOW)] == ["open_link"]


def test_later_items_get_a_calendar_step_but_nothing_else(settings):
    picture = Decision(category="later", reason="Event", summary="Picture day next Thursday.",
                       date="2026-11-19", event_title="Picture day")
    steps = build_steps(_item("Picture day is next Thursday."), picture, _family(settings), [], now=NOW)
    assert [s["type"] for s in steps] == ["add_calendar"]


# ---- the Android shortcuts behind the steps --------------------------------------------

def test_shortcut_commands_are_quoted_for_the_phone_shell(settings):
    settings.adb_serial = "R58M"
    cmd = AndroidShortcuts(settings).command("am", "start", "--es", "sms_body", "Anna's sick; I'm on it")
    assert cmd[:4] == ["adb", "-s", "R58M", "shell"]
    assert cmd[4] == "am start --es sms_body 'Anna'\"'\"'s sick; I'\"'\"'m on it'"


def test_shortcuts_report_phone_errors(settings):
    def refuse(cmd, **kwargs):
        return CompletedProcess(cmd, 0, stdout="Starting: Intent { act=android.intent.action.SET_ALARM }\n"
                                               "Error: Activity not started, unable to resolve Intent", stderr="")
    result = AndroidShortcuts(settings, runner=refuse).set_alarm(12, 30, "Leave now")
    assert not result.ok and "unable to resolve Intent" in result.detail


def test_calendar_times():
    begin, end, all_day = calendar_times({"date": "2026-11-19", "time": ""})
    assert all_day and end - begin == 24 * 3600 * 1000
    begin, end, all_day = calendar_times({"date": "2026-11-19", "time": "16:30"})
    assert not all_day and end - begin == 3600 * 1000


def test_mobilerun_phone_opens_the_app_then_taps_send(settings):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return CompletedProcess(cmd, 0, stdout="Starting: Intent\n", stderr="")

    phone = MobilerunPhone(_family(settings), runner=run)
    step = {"id": "partner", "type": "text_partner", "to": "+14155550199", "name": "Sam",
            "text": "Heads up: Anna needs pickup.", "label": "Text Sam"}
    result = phone.perform_step(_item(), step)
    assert result.ok and result.detail == "Text sent to Sam."
    shells = [c[-1] for c in calls if "shell" in c]
    assert any("android.intent.action.SENDTO" in s and "smsto:+14155550199" in s for s in shells)
    agent = [c for c in calls if c[1:2] == ["run"]][0]
    assert "Tap the Send button once" in agent[2] and "Heads up: Anna needs pickup." in agent[2]


def test_mobilerun_phone_sets_an_alarm_without_the_agent(settings):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return CompletedProcess(cmd, 0, stdout="Starting: Intent\n", stderr="")

    step = {"id": "alarm", "type": "set_alarm", "time": "12:30", "message": "Leave now", "label": "alarm"}
    assert MobilerunPhone(settings, runner=run).perform_step(_item(), step).ok
    assert not any(c[1:2] == ["run"] for c in calls)              # no AI needed for an alarm
    assert any("SET_ALARM" in c[-1] and "HOUR 12" in c[-1] and "MINUTES 30" in c[-1] for c in calls)


# ---- running the plan ------------------------------------------------------------------

class PlanClassifier:
    def __init__(self, decision):
        self.decision = decision

    def classify(self, app, sender, text, also_seen_in=None):
        return self.decision.model_copy()


def test_every_ticked_step_runs_after_one_press(make_engine, settings):
    _family(settings)
    engine = make_engine(FakeVoice())
    engine.classifier = PlanClassifier(FEVER.model_copy(update={"date": "", "time": ""}))
    item, _ = engine.ingest(IncomingMessage("Remind", "Ms. Rivera", "Anna has a fever. Please pick her up."))
    assert [s["type"] for s in engine.store.get_item(item.id).steps] == ["reply", "text_partner", "directions"]
    engine.resume(item.id, True, "call")
    done = engine.store.get_item(item.id)
    assert done.status == "done" and all(s["status"] == "done" for s in done.steps)
    assert len(engine.phone.goals) == 1 and [s["type"] for s in engine.phone.steps] == ["text_partner", "directions"]


def test_unticked_steps_are_skipped(make_engine, settings):
    _family(settings)
    engine = make_engine(FakeVoice())
    engine.classifier = PlanClassifier(FEVER.model_copy(update={"date": "", "time": ""}))
    item, _ = engine.ingest(IncomingMessage("Remind", "Ms. Rivera", "Anna has a fever. Please pick her up."))
    engine.resume(item.id, True, "page", skip=["partner"])
    steps = {s["id"]: s["status"] for s in engine.store.get_item(item.id).steps}
    assert steps == {"main": "done", "partner": "skipped", "directions": "done"}
    assert [s["type"] for s in engine.phone.steps] == ["directions"]


def test_add_to_calendar_from_a_later_message(make_engine, settings):
    engine = make_engine()
    engine.classifier = PlanClassifier(Decision(category="later", reason="Event", summary="Picture day.",
                                                date="2099-11-19", event_title="Picture day"))
    item, _ = engine.ingest(IncomingMessage("Gmail", "Lincoln Elementary", "Picture day is next Thursday."))
    assert engine.store.get_item(item.id).status == "later"
    row = engine.coming_up(days=100000)[0]
    assert row["title"] == "Picture day" and row["can_add"] and not row["on_calendar"]
    assert engine.run_step_now(item.id, "calendar")
    assert engine.store.get_item(item.id).steps[0]["status"] == "done"
    assert engine.coming_up(days=100000)[0]["on_calendar"]
    assert engine.run_step_now(item.id, "calendar") is False       # already on the calendar


def test_old_databases_get_the_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE items (id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, "
                "app TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL, "
                "category TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', "
                "child TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '', action_type TEXT NOT NULL DEFAULT 'none', "
                "reply_text TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '', instructions TEXT NOT NULL DEFAULT '', "
                "rule_hit TEXT NOT NULL DEFAULT '', sorted_by TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', "
                "sources TEXT NOT NULL DEFAULT '[]', duplicate_count INTEGER NOT NULL DEFAULT 0, "
                "call_attempts INTEGER NOT NULL DEFAULT 0)")
    con.execute("INSERT INTO items (id, created_at, updated_at, app, sender, text, status) "
                "VALUES ('old1', 1, 1, 'Remind', 't', 'hello', 'later')")
    con.commit()
    con.close()
    store = Store(path)
    old = store.get_item("old1")
    assert old.steps == [] and old.event_date == ""
    store.update_item("old1", steps=[{"id": "calendar"}], event_date="2026-11-19")
    assert store.get_item("old1").steps == [{"id": "calendar"}]
