import time

from conftest import FakeVoice

from hushbell.models import IncomingMessage

BAKE = IncomingMessage("Remind", "Ms. Rivera · Room 12", "Bake sale Friday! Bring cookies.")
FORM = IncomingMessage("Remind", "Ms. Rivera · Room 12", "Field trip form due TODAY: https://forms.gle/fieldtrip12")
REPOST = IncomingMessage("WhatsApp", "3rd grade parents", "Jen: Reminder: field trip form due today!")
FEVER = IncomingMessage("Remind", "Ms. Rivera · Room 12", "Maya has a fever (101°F). Please pick her up by 1pm.")


def test_noise_is_saved_for_later(make_engine):
    engine = make_engine()
    item, new = engine.ingest(BAKE)
    item = engine.store.get_item(item.id)
    assert new and item.status == "later" and item.category == "later"
    assert engine.phone.goals == []


def test_needs_you_waits_then_fills_the_form_after_approval(make_engine):
    engine = make_engine()
    item, _ = engine.ingest(FORM)
    assert engine.store.get_item(item.id).status == "waiting"
    assert engine.phone.goals == []                       # nothing happens before the OK
    assert engine.resume(item.id, True, "page")
    item = engine.store.get_item(item.id)
    assert item.status == "done"
    assert "https://forms.gle/fieldtrip12" in engine.phone.goals[0]
    assert "Never enter passwords, card numbers or payment details" in engine.phone.goals[0]
    assert engine.resume(item.id, True, "page") is False  # can't approve twice


def test_repost_joins_the_earlier_item(make_engine):
    engine = make_engine()
    first, _ = engine.ingest(FORM)
    second, new = engine.ingest(REPOST)
    assert not new and second.id == first.id
    item = engine.store.get_item(first.id)
    assert item.sources == ["Remind", "WhatsApp"] and item.duplicate_count == 1
    assert len(engine.store.list_items()) == 1


def test_repeated_urgent_message_joins_an_open_alert_but_rings_again_once_closed(make_engine):
    voice = FakeVoice()
    engine = make_engine(voice)
    first, _ = engine.ingest(FEVER)
    again, new = engine.ingest(FEVER)                      # alert still open: one call, not two
    assert not new and again.id == first.id and len(voice.calls) == 1
    engine.resume(first.id, True, "call")                  # parent handled it
    follow_up, new = engine.ingest(FEVER)                  # the school asks again later
    assert new and follow_up.id != first.id
    assert len(voice.calls) == 2


def test_safety_rules_beat_the_model_and_call_the_parent(make_engine):
    voice = FakeVoice()
    engine = make_engine(voice)
    item, _ = engine.ingest(FEVER)                         # the fake model says "later"
    item = engine.store.get_item(item.id)
    assert item.category == "urgent" and "fever" in item.rule_hit
    assert item.status == "calling" and voice.calls == [item.id]
    assert item.reply_text == "Thank you for letting me know. I'm on my way."
    assert engine.resume(item.id, True, "call")
    item = engine.store.get_item(item.id)
    assert item.status == "done"
    assert '"Thank you for letting me know. I\'m on my way."' in engine.phone.goals[0]


def test_without_calls_urgent_waits_at_the_top_of_the_page(make_engine):
    engine = make_engine(None)
    item, _ = engine.ingest(FEVER)
    assert engine.store.get_item(item.id).status == "waiting"


def test_failed_call_does_not_lose_the_item(make_engine):
    voice = FakeVoice()
    voice.fail = True
    engine = make_engine(voice)
    item, _ = engine.ingest(FEVER)
    assert engine.store.get_item(item.id).status == "waiting"


def test_missed_call_redials_once_then_waits(make_engine):
    voice = FakeVoice()
    engine = make_engine(voice)
    item, _ = engine.ingest(FEVER)
    engine.call_status(item.id, "no-answer")
    deadline = time.time() + 2
    while len(voice.calls) < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert len(voice.calls) == 2
    engine.call_status(item.id, "no-answer")
    assert engine.store.get_item(item.id).status == "waiting"


def test_parent_says_they_will_handle_it(make_engine):
    engine = make_engine(FakeVoice())
    item, _ = engine.ingest(FEVER)
    assert engine.resume(item.id, False, "call")
    assert engine.store.get_item(item.id).status == "skipped"
    assert engine.phone.goals == []


def test_when_claude_is_down_nothing_is_lost(make_engine):
    engine = make_engine()
    engine.classifier.fail = True
    calm, _ = engine.ingest(BAKE)
    assert engine.store.get_item(calm.id).category == "needs_you"     # safe default: show it
    sick, _ = engine.ingest(FEVER)
    assert engine.store.get_item(sick.id).category == "urgent"        # rules still work offline


def test_unexpected_sorting_errors_also_fail_safe(make_engine):
    engine = make_engine()
    engine.classifier.fail = "crash"
    item, _ = engine.ingest(FEVER)
    item = engine.store.get_item(item.id)
    assert item.category == "urgent" and item.status == "waiting"


def test_missing_claude_credentials_fail_safe(settings, monkeypatch):
    from hushbell.triage.classifier import ClassifierError, ClaudeClassifier

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    class NoAuth:
        class beta:
            class messages:
                @staticmethod
                def create(**kwargs):
                    raise TypeError("Could not resolve authentication method")

    import pytest
    with pytest.raises(ClassifierError):
        ClaudeClassifier(settings, client=NoAuth()).classify("Remind", "t", "hello")


def test_unsafe_links_are_never_opened(make_engine):
    engine = make_engine()
    item, _ = engine.ingest(IncomingMessage("Remind", "Unknown", "Click javascript link to sign"))
    engine.resume(item.id, True, "page")
    item = engine.store.get_item(item.id)
    assert item.status == "failed" and "not a web address" in item.result
    assert engine.phone.goals == []


def test_parent_can_change_the_reply_before_it_is_sent(make_engine):
    engine = make_engine(FakeVoice())
    item, _ = engine.ingest(FEVER)
    assert engine.resume(item.id, True, "page", reply_text="  Leaving now,   there in 20 minutes. ")
    item = engine.store.get_item(item.id)
    assert item.reply_text == "Leaving now, there in 20 minutes."
    assert '"Leaving now, there in 20 minutes."' in engine.phone.goals[0]


class FlakyPhone:
    name = "flaky"

    def __init__(self):
        self.attempts = 0

    def perform(self, item):
        from hushbell.phone import ActionResult

        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("usb unplugged")
        return ActionResult(True, "Sent.")


def test_failed_phone_step_can_be_tried_again(make_engine):
    engine = make_engine()
    engine.phone = FlakyPhone()
    item, _ = engine.ingest(FORM)
    engine.resume(item.id, True, "page")
    failed = engine.store.get_item(item.id)
    assert failed.status == "failed" and "usb unplugged" in failed.result
    assert engine.retry(item.id)
    assert engine.store.get_item(item.id).status == "done"
    assert engine.retry(item.id) is False                  # nothing left to retry


def test_restart_picks_up_work_in_progress(make_engine):
    engine = make_engine()
    unsorted = engine.store.create_item("Remind", "Ms. Rivera", "Bake sale Friday! Bring cookies.")
    ringing = engine.store.create_item("Remind", "Ms. Rivera", "x", status="calling")
    midway = engine.store.create_item("Remind", "Ms. Rivera", "y", status="acting")
    assert engine.recover() == 3
    assert engine.store.get_item(unsorted.id).status == "later"        # sorted now
    assert engine.store.get_item(ringing.id).status == "waiting"       # back on the page
    assert engine.store.get_item(midway.id).status == "failed"         # the parent decides whether to retry
    assert engine.recover() == 0


def test_state_counts(make_engine):
    engine = make_engine()
    engine.ingest(BAKE)
    engine.ingest(FORM)
    engine.ingest(REPOST)
    s = engine.state()
    assert s["counts"]["messages_today"] == 3
    assert s["counts"]["needs_you"] == 1
    assert len(s["digest"]["later"]) == 1
