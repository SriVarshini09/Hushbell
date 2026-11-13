"""Wiring: build the engine from settings and run the background loops."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time

from hushbell.config import Settings
from hushbell.engine import Engine
from hushbell.ingest.adb import AdbNotificationSource
from hushbell.phone import make_phone
from hushbell.store import Store
from hushbell.triage.classifier import ClaudeClassifier
from hushbell.voice import make_voice

log = logging.getLogger(__name__)


def build_engine(settings: Settings) -> Engine:
    from langgraph.checkpoint.sqlite import SqliteSaver

    store = Store(settings.db_path)
    checkpointer = SqliteSaver(sqlite3.connect(str(settings.graph_db_path), check_same_thread=False))
    from hushbell.assistant import Assistant

    voice = make_voice(settings)
    return Engine(
        settings=settings,
        store=store,
        classifier=ClaudeClassifier(settings),
        # A simulated (rehearsal) voice can't text or call anyone, so it is never the messenger.
        phone=make_phone(settings, messenger=None if getattr(voice, "simulated", False) else voice),
        voice=voice,
        checkpointer=checkpointer,
        assistant=Assistant(settings),
    )


def make_reader_source(engine: Engine, cloud: bool):
    """Where notifications are read from: a USB Android (adb), or a PhoneUse cloud phone."""
    settings = engine.settings
    if not cloud:
        return AdbNotificationSource(settings, store=engine.store)
    from hushbell.phoneuse import PhoneUseClient, PhoneUseNotificationSource, ScreenReader

    # Share the actions' client, so reading waits while a task runs on the cloud phone.
    client = getattr(engine.phone, "client", None) or PhoneUseClient(settings.phoneuse_api_key, settings.phoneuse_url)
    return PhoneUseNotificationSource(settings, client, ScreenReader(settings), store=engine.store)


class PhoneReader(threading.Thread):
    """Polls the phone's notifications and feeds new school messages to the engine."""

    def __init__(self, engine: Engine, source: AdbNotificationSource, interval: float | None = None):
        super().__init__(name="hushbell-phone-reader", daemon=True)
        self.engine = engine
        self.source = source
        self.interval = interval or engine.settings.poll_seconds
        self.stop_event = threading.Event()
        self._last_error = ""

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                for message in self.source.poll():
                    self.engine.ingest(message)
                self.engine.health["phone"] = "ok"
                if self._last_error:
                    self.engine.store.add_event("phone", "Reading the phone again")
                    self._last_error = ""
            except Exception as exc:  # noqa: BLE001 - keep polling; report once per new error
                error = str(exc)[:200]
                self.engine.health["phone"] = "not connected"
                if error != self._last_error:
                    log.warning("Can't read the phone: %s", error)
                    self.engine.store.add_event("phone", f"Can't read the phone: {error}")
                    self._last_error = error
            self.stop_event.wait(self.interval)


class DigestClock(threading.Thread):
    """Writes the evening digest into the activity log once a day at HUSHBELL_DIGEST_TIME."""

    def __init__(self, engine: Engine):
        super().__init__(name="hushbell-digest", daemon=True)
        self.engine = engine
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("Digest failed")
            self.stop_event.wait(30)

    def tick(self, now: time.struct_time | None = None) -> bool:
        now = now or time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if time.strftime("%H:%M", now) < self.engine.settings.digest_time:
            return False
        store = self.engine.store
        if store.get_kv("last_digest") == today:
            return False
        start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
        d = self.engine.digest(since=start)
        text = (f"{d['messages']} school messages today. {len(d['handled'])} handled for you, "
                f"{len(d['open'])} still waiting, {len(d['later'])} saved for later.")
        if d["later"]:
            text += " Later: " + "; ".join(d["later"][:8])
        store.add_event("digest", text)
        store.set_kv("last_digest", today)
        return True
