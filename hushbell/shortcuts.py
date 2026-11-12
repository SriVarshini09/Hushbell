"""Open the phone's own apps already filled in, using standard Android intents over USB.

These need no AI. An alarm is set directly and directions open directly. A calendar
event or a text message opens filled in, and the phone agent only has to tap Save or Send.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from hushbell.config import Settings


@dataclass
class ShortcutResult:
    ok: bool
    detail: str = ""


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:240] if lines else ""


class AndroidShortcuts:
    def __init__(self, settings: Settings, runner=subprocess.run):
        self.settings = settings
        self._run = runner

    def command(self, *args: str) -> list[str]:
        cmd = [self.settings.adb_path]
        if self.settings.adb_serial:
            cmd += ["-s", self.settings.adb_serial]
        # `adb shell` joins its arguments into one command line for the phone's shell, so quote each part.
        return cmd + ["shell", " ".join(shlex.quote(str(a)) for a in args)]

    def _shell(self, *args: str) -> ShortcutResult:
        try:
            result = self._run(self.command(*args), capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=20)
        except FileNotFoundError:
            return ShortcutResult(False, "adb is not installed, so Hushbell can't reach the phone.")
        except subprocess.TimeoutExpired:
            return ShortcutResult(False, "The phone didn't answer in time. Is it still plugged in?")
        output = f"{result.stdout}\n{result.stderr}"
        if result.returncode != 0 or "Error:" in output or "Exception" in output:
            return ShortcutResult(False, _last_line(output) or "The phone refused the request.")
        return ShortcutResult(True)

    def wake(self) -> None:
        self._shell("input", "keyevent", "KEYCODE_WAKEUP")

    def set_alarm(self, hour: int, minute: int, message: str) -> ShortcutResult:
        return self._shell("am", "start", "-a", "android.intent.action.SET_ALARM",
                           "--ei", "android.intent.extra.alarm.HOUR", str(hour),
                           "--ei", "android.intent.extra.alarm.MINUTES", str(minute),
                           "--es", "android.intent.extra.alarm.MESSAGE", message,
                           "--ez", "android.intent.extra.alarm.SKIP_UI", "true")

    def open_calendar_event(self, title: str, begin_ms: int, end_ms: int, all_day: bool,
                            description: str) -> ShortcutResult:
        return self._shell("am", "start", "-a", "android.intent.action.INSERT",
                           "-d", "content://com.android.calendar/events",
                           "--es", "title", title, "--es", "description", description,
                           "--el", "beginTime", str(begin_ms), "--el", "endTime", str(end_ms),
                           "--ez", "allDay", "true" if all_day else "false")

    def open_text(self, number: str, text: str) -> ShortcutResult:
        return self._shell("am", "start", "-a", "android.intent.action.SENDTO", "-d", f"smsto:{number}",
                           "--es", "sms_body", text)

    def open_directions(self, destination: str) -> ShortcutResult:
        url = ("https://www.google.com/maps/dir/?api=1&travelmode=driving&destination="
               + quote(destination, safe=""))
        return self._shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)


def calendar_times(step: dict) -> tuple[int, int, bool]:
    """Start and end of a calendar step in milliseconds, and whether it is all day."""
    day = date.fromisoformat(step["date"])
    clock = step.get("time") or ""
    if clock:
        hour, minute = (int(x) for x in clock.split(":"))
        start = datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)
        return int(start.timestamp() * 1000), int((start + timedelta(hours=1)).timestamp() * 1000), False
    # Android stores all-day events at midnight UTC.
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000), True
