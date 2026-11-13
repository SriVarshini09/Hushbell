"""Read notifications from an Android phone over USB (ADB).

Hushbell runs `adb shell dumpsys notification --noredact` every few seconds,
parses the active notifications, keeps the ones from school apps, and hands new
ones to the engine. No app has to be installed on the phone for this part.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass

from hushbell.config import Settings
from hushbell.models import IncomingMessage

log = logging.getLogger(__name__)

_RECORD_START = re.compile(r"NotificationRecord\(")
_PKG = re.compile(r"\bpkg=([\w.]+)")
_HEADER_KEY = re.compile(r"\bkey=(\S+?): Notification\(")
_LINE_KEY = re.compile(r"^\s*key=(\S+)\s*$")
_EXTRA = re.compile(r"^\s*android\.(title|text|bigText|subText|conversationTitle|summaryText)=(.*)$")
_TYPED_VALUE = re.compile(r"^\w+(?:\[\])? \((.*)$")

# Text that only says "you have messages" carries nothing worth sorting.
_SUMMARY_ONLY = re.compile(r"^\s*\d+\s+(new\s+)?(messages?|notifications?|emails?|chats?)\s*$", re.I)


@dataclass
class RawNotification:
    package: str
    key: str
    title: str = ""
    text: str = ""
    big_text: str = ""
    sub_text: str = ""
    conversation_title: str = ""

    @property
    def body(self) -> str:
        return (self.big_text or self.text).strip()

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha1(f"{self.title}\n{self.body}".encode("utf-8")).hexdigest()[:12]
        return f"{self.key}#{digest}"


def parse_dumpsys(output: str) -> list[RawNotification]:
    """Parse the active notification records from `dumpsys notification --noredact`."""
    records: list[RawNotification] = []
    current: RawNotification | None = None
    pending_field: str | None = None
    pending_value: list[str] = []

    def finish_field() -> None:
        nonlocal pending_field, pending_value
        if current is not None and pending_field:
            _set_extra(current, pending_field, "\n".join(pending_value))
        pending_field, pending_value = None, []

    for line in output.splitlines():
        if pending_field is not None:
            stripped = line.rstrip()
            if stripped.endswith(")"):
                pending_value.append(stripped[:-1])
                finish_field()
            else:
                pending_value.append(stripped)
            continue

        if _RECORD_START.search(line):
            if current is not None:
                records.append(current)
            pkg = _PKG.search(line)
            key = _HEADER_KEY.search(line)
            current = RawNotification(package=pkg.group(1) if pkg else "", key=key.group(1) if key else "")
            continue

        if current is None:
            continue

        key_line = _LINE_KEY.match(line)
        if key_line and not current.key:
            current.key = key_line.group(1)
            continue

        extra = _EXTRA.match(line)
        if not extra:
            continue
        name, raw = extra.group(1), extra.group(2).rstrip()
        if raw == "null":
            continue
        typed = _TYPED_VALUE.match(raw)
        if not typed:
            continue
        value = typed.group(1)
        if value.endswith(")"):
            _set_extra(current, name, value[:-1])
        else:
            pending_field, pending_value = name, [value]

    finish_field()
    if current is not None:
        records.append(current)
    return [r for r in records if r.package]


def _set_extra(record: RawNotification, name: str, value: str) -> None:
    value = value.strip()
    if name == "title":
        record.title = value
    elif name == "text":
        record.text = value
    elif name == "bigText":
        record.big_text = value
    elif name == "subText":
        record.sub_text = value
    elif name == "conversationTitle":
        record.conversation_title = value


def to_message(record: RawNotification, settings: Settings) -> IncomingMessage | None:
    """Keep only school messages. Returns None for anything Hushbell should not read."""
    app = settings.apps.get(record.package)
    if not app:
        return None
    label, kind = app
    body = record.body
    if not body or _SUMMARY_ONLY.match(body):
        return None
    sender = record.conversation_title or record.title or label
    if kind == "mixed":
        haystack = f"{sender}\n{record.title}\n{body}".lower()
        if not any(k.lower() in haystack for k in settings.keywords):
            return None  # personal chat or email: never read it
    return IncomingMessage(app=label, sender=sender, text=body, package=record.package,
                           key=record.fingerprint, source="phone")


FORGET_AFTER_POLLS = 3


class AdbNotificationSource:
    """Polls the phone and returns school messages it has not seen before."""

    def __init__(self, settings: Settings, runner=subprocess.run, store=None):
        self.settings = settings
        self._run = runner
        self.store = store
        # What Hushbell has already looked at is kept across restarts, so a message that
        # arrives while Hushbell is off is still picked up the next time it starts.
        self._seen: set[str] = store.load_seen() if store else set()
        ran_before = bool(store and store.get_kv("phone_primed") == "1")
        # On the very first run, the first poll only marks what's already on the phone.
        self._primed = settings.backfill or ran_before
        self._absent: dict[str, int] = {}

    def adb_args(self, *args: str) -> list[str]:
        cmd = [self.settings.adb_path]
        if self.settings.adb_serial:
            cmd += ["-s", self.settings.adb_serial]
        return cmd + list(args)

    def available(self) -> bool:
        return shutil.which(self.settings.adb_path) is not None

    def read_raw(self) -> str:
        result = self._run(
            self.adb_args("shell", "dumpsys", "notification", "--noredact"),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "adb failed").strip()[:300])
        return result.stdout

    def read_records(self) -> list[RawNotification] | None:
        """The notifications on the phone now (None: couldn't look this time, try again later)."""
        return parse_dumpsys(self.read_raw())

    def poll(self) -> list[IncomingMessage]:
        records = self.read_records()
        if records is None:
            return []
        fresh: list[IncomingMessage] = []
        new_fingerprints: list[str] = []
        for record in records:
            if record.fingerprint in self._seen:
                continue
            self._seen.add(record.fingerprint)
            new_fingerprints.append(record.fingerprint)
            message = to_message(record, self.settings)
            if message and self._primed:
                fresh.append(message)
        gone = self._forget_cleared({r.fingerprint for r in records})
        if self.store:
            self.store.mark_seen(new_fingerprints)
            self.store.forget_seen(gone)
        if not self._primed:
            self._primed = True
            if self.store:
                self.store.set_kv("phone_primed", "1")
            log.info("Phone connected. Skipped %d notifications that were already there.", len(records))
        return fresh

    def _forget_cleared(self, present: set[str]) -> list[str]:
        """Forget notifications that have been gone from the phone for a few polls.

        Once the parent clears a notification, the same words arriving again are a new
        message (a teacher repeating herself, or a second demo run), not one already seen.
        Waiting a few polls avoids treating a brief flicker as a new message.
        """
        gone: list[str] = []
        for fingerprint in list(self._seen):
            if fingerprint in present:
                self._absent.pop(fingerprint, None)
                continue
            misses = self._absent.get(fingerprint, 0) + 1
            if misses >= FORGET_AFTER_POLLS:
                gone.append(fingerprint)
                self._absent.pop(fingerprint, None)
                self._seen.discard(fingerprint)
            else:
                self._absent[fingerprint] = misses
        return gone
