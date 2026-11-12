from pathlib import Path
from subprocess import CompletedProcess

from hushbell.ingest.adb import AdbNotificationSource, parse_dumpsys, to_message

FIXTURE = (Path(__file__).parent / "fixtures" / "dumpsys_notification.txt").read_text(encoding="utf-8")


def test_parse_reads_every_record():
    records = parse_dumpsys(FIXTURE)
    assert [r.package for r in records] == [
        "com.remind101", "com.whatsapp", "com.whatsapp", "com.google.android.gm",
        "com.google.android.gm", "com.android.systemui",
    ]
    remind = records[0]
    assert remind.title == "Ms. Rivera · Room 12"
    assert remind.body == "Maya has a fever (101°F). Please pick her up by 1pm."
    assert remind.key == "0|com.remind101|1042|null|10213"
    assert remind.sub_text == ""


def test_parse_handles_tags_with_colons_and_multiline_text():
    gmail = parse_dumpsys(FIXTURE)[3]
    assert gmail.key == "0|com.google.android.gm|0|gig:12345:parent.demo@gmail.com|10150"
    assert gmail.body == "Weekly newsletter: Nov 12\nPicture day is Thursday. Book fair all week."


def test_only_school_messages_are_read(settings):
    messages = [to_message(r, settings) for r in parse_dumpsys(FIXTURE)]
    kept = [(m.app, m.sender) for m in messages if m]
    assert kept == [
        ("Remind", "Ms. Rivera · Room 12"),
        ("WhatsApp", "3rd grade parents"),     # group named in HUSHBELL_KEYWORDS
        ("Gmail", "Lincoln Elementary"),       # school named in HUSHBELL_KEYWORDS
    ]
    # The personal WhatsApp chat, the "3 new messages" summary and system notices are never read.


def test_mixed_apps_are_ignored_without_keywords(settings):
    settings.keywords = []
    kept = [m for m in (to_message(r, settings) for r in parse_dumpsys(FIXTURE)) if m]
    assert [m.app for m in kept] == ["Remind"]


def _runner(outputs):
    def run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, stdout=outputs.pop(0), stderr="")
    return run


def test_first_poll_skips_what_was_already_there(settings):
    new_msg = FIXTURE.replace("Bake", "Bake").replace(
        "Snoozed notifications:",
        "    NotificationRecord(0x1: pkg=com.remind101 user=UserHandle{0} id=9 key=0|com.remind101|9|null|10213: Notification(flags=0x10))\n"
        "      notification=\n        extras={\n          android.title=String (Ms. Rivera · Room 12)\n"
        "          android.text=String (Bake sale Friday! Bring cookies.)\n        }\n  Snoozed notifications:",
    )
    source = AdbNotificationSource(settings, runner=_runner([FIXTURE, new_msg, new_msg]))
    assert source.poll() == []
    second = source.poll()
    assert [m.text for m in second] == ["Bake sale Friday! Bring cookies."]
    assert source.poll() == []


def test_messages_that_arrive_while_hushbell_is_off_are_picked_up(settings):
    from hushbell.store import Store

    store = Store(":memory:")
    first_run = AdbNotificationSource(settings, runner=_runner([""]), store=store)
    assert first_run.poll() == []                          # very first run: nothing on the phone yet

    # Hushbell is stopped. A fever message arrives. Hushbell starts again.
    after_restart = AdbNotificationSource(settings, runner=_runner([FIXTURE, FIXTURE]), store=store)
    texts = [m.text for m in after_restart.poll()]
    assert "Maya has a fever (101°F). Please pick her up by 1pm." in texts
    assert after_restart.poll() == []                      # and only once

    third_start = AdbNotificationSource(settings, runner=_runner([FIXTURE]), store=store)
    assert third_start.poll() == []                        # already seen before the restart


def test_adb_command_uses_serial(settings):
    settings.adb_serial = "R58M123"
    assert AdbNotificationSource(settings).adb_args("shell", "x") == ["adb", "-s", "R58M123", "shell", "x"]


def test_a_cleared_notification_can_arrive_again_as_new(settings):
    """The teacher sends the same words twice (or a demo is rehearsed twice): the second one counts."""
    outputs = [FIXTURE, "", FIXTURE, "", "", "", FIXTURE, FIXTURE]
    settings.backfill = True
    source = AdbNotificationSource(settings, runner=_runner(outputs))
    assert len(source.poll()) == 3            # first sight
    assert source.poll() == []                # gone for one poll...
    assert source.poll() == []                # ...and back: a flicker, not a new message
    for _ in range(3):
        assert source.poll() == []            # cleared from the phone for three polls
    assert len(source.poll()) == 3            # the same words again are new messages
    assert source.poll() == []
