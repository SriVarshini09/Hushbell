"""Acting on the parent's phone: reply inside a school app, or fill in a form.

The default backend drives the phone with Mobilerun (open source, MIT), through its
command line. Hushbell only ever runs an action after the parent said yes.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from hushbell.config import Settings
from hushbell.models import Item

log = logging.getLogger(__name__)


@dataclass
class ActionResult:
    ok: bool
    detail: str


class PhoneBackend(Protocol):
    name: str

    def perform(self, item: Item) -> ActionResult: ...


class UnsafeAction(Exception):
    pass


def build_goal(item: Item, settings: Settings) -> str:
    """Turn an approved item into one plain instruction for the phone agent."""
    if item.action_type == "reply":
        if not item.reply_text.strip():
            raise UnsafeAction("There is no reply text to send.")
        snippet = " ".join(item.text.split())[:60]
        return (
            f'In the {item.app} app, open the conversation with "{item.sender}" '
            f'(their latest message starts with "{snippet}"). '
            f'Type exactly this reply and send it: "{item.reply_text}". '
            "Do not change the wording, do not send anything else, and do not open other apps."
        )
    if item.action_type == "open_link":
        url = item.url.strip()
        if not url.lower().startswith(("https://", "http://")):
            raise UnsafeAction("The form link is not a web address, so Hushbell will not open it.")
        details = []
        if item.child:
            details.append(f"child's name: {item.child}")
        elif settings.children:
            details.append(f"child's name: {settings.children[0]}")
        if settings.parent_name:
            details.append(f"parent's name: {settings.parent_name}")
        return (
            f"Open this link in Chrome: {url} . {item.instructions} "
            f"Use these details where the form asks for them: {'; '.join(details) or 'none'}. "
            "Fill in only what you know. Never enter passwords, card numbers or payment details; "
            "if the form asks for any of those, stop without submitting. "
            "Submit the form once every required field is filled, then stop."
        )
    raise UnsafeAction("This item has nothing for the phone to do.")


class DryRunPhone:
    """Logs what it would do. Useful for trying Hushbell without a phone attached."""

    name = "dry-run"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.goals: list[str] = []

    def perform(self, item: Item) -> ActionResult:
        goal = build_goal(item, self.settings)
        self.goals.append(goal)
        log.info("[dry run] would ask the phone: %s", goal)
        return ActionResult(True, "Dry run: nothing was sent from the phone.")


class MobilerunPhone:
    """Runs `mobilerun run "<goal>"`, or replays a recorded macro when one is configured."""

    name = "mobilerun"

    def __init__(self, settings: Settings, runner=subprocess.run):
        self.settings = settings
        self._run = runner

    def _base_cmd(self) -> list[str]:
        cmd = self.settings.mobilerun_cmd
        if os.path.exists(cmd):  # a full path, possibly with spaces in it
            return [cmd]
        return shlex.split(cmd, posix=os.name != "nt")

    def available(self) -> bool:
        return shutil.which(self._base_cmd()[0]) is not None

    def perform(self, item: Item) -> ActionResult:
        macro = self.settings.macros.get(f"{item.app.lower()}:{item.action_type}")
        if macro and item.action_type == "open_link":
            cmd = self._base_cmd() + ["macro", "replay", macro, "--on-mismatch", "agent"]
        else:
            # Recorded macros replay their typed text word for word, so replies always use the agent.
            cmd = self._base_cmd() + ["run", build_goal(item, self.settings),
                                      "--steps", str(self.settings.mobilerun_steps)]
            if self.settings.mobilerun_reasoning:
                cmd.append("--reasoning")
            # Left empty, Mobilerun uses whatever `mobilerun configure` saved.
            if self.settings.mobilerun_provider:
                cmd += ["--provider", self.settings.mobilerun_provider]
            if self.settings.mobilerun_model:
                cmd += ["--model", self.settings.mobilerun_model]
        if self.settings.adb_serial:
            cmd += ["--device", self.settings.adb_serial]
        if self.settings.adb_serial:
            env = {**os.environ, "ANDROID_SERIAL": self.settings.adb_serial}
        else:
            env = None
        log.info("Phone agent starting: %s", item.action_type)
        try:
            result = self._run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=self.settings.mobilerun_timeout, env=env)
        except FileNotFoundError:
            return ActionResult(False, "Mobilerun is not installed. Run: uv tool install mobilerun")
        except subprocess.TimeoutExpired:
            return ActionResult(False, f"The phone agent took longer than {self.settings.mobilerun_timeout} s and was stopped.")
        tail = _last_line(result.stdout) or _last_line(result.stderr)
        if result.returncode != 0:
            return ActionResult(False, f"The phone agent stopped with an error: {tail}")
        return ActionResult(True, tail or "Done on the phone.")


def _last_line(text: str | None) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:240] if lines else ""


def make_phone(settings: Settings) -> PhoneBackend:
    if settings.phone_backend == "dry-run":
        return DryRunPhone(settings)
    return MobilerunPhone(settings)
