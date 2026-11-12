"""`hushbell init`: ask a few questions and write the answers to .env."""

from __future__ import annotations

import getpass
import re
from pathlib import Path

QUESTIONS: list[tuple[str, str, bool]] = [
    # (env name, question, is a secret)
    ("ANTHROPIC_API_KEY", "Claude API key (from console.anthropic.com)", True),
    ("HUSHBELL_CHILDREN", "Your children's first names, separated by commas", False),
    ("HUSHBELL_PARENT_NAME", "Your name, as you'd sign a school form", False),
    ("HUSHBELL_SCHOOLS", "School names, separated by commas", False),
    ("HUSHBELL_KEYWORDS", "Words that mark a WhatsApp or Gmail message as school-related "
                          "(school name, parent group name), separated by commas", False),
    ("HUSHBELL_SCHOOL_ADDRESS", "School address, for directions when you need to pick up (Enter to skip)", False),
    ("HUSHBELL_PARTNER_NAME", "A co-parent Hushbell can text in an emergency, with your OK: their name (Enter to skip)",
     False),
    ("HUSHBELL_PARTNER_PHONE", "Their mobile number, like +14155550199 (Enter to skip)", False),
    ("HUSHBELL_PARENT_PHONE", "Your mobile number for urgent calls, like +14155550123 (Enter to skip calls)", False),
    ("TWILIO_ACCOUNT_SID", "Twilio Account SID (Enter to skip)", False),
    ("TWILIO_AUTH_TOKEN", "Twilio Auth Token (Enter to skip)", True),
    ("TWILIO_FROM_NUMBER", "Your Twilio phone number, like +14155550100 (Enter to skip)", False),
]


_ONE_KEY = re.compile(r"sk-ant-(?:(?!sk-ant-)[A-Za-z0-9_\-])+")


def clean_api_key(text: str) -> tuple[str, str]:
    """Pull one Claude API key out of what was typed or pasted.

    Returns (key, note). The key is "" when the text doesn't hold a usable key.
    Hidden input makes it easy to paste a key several times, or with other text
    after it, so those cases are repaired instead of saved as they are.
    """
    text = text.strip().strip("\"'")
    found = _ONE_KEY.findall(text)
    if not found:
        return "", "That doesn't look like a Claude API key (they start with sk-ant-). Not saved."
    if len(found) == 1:
        key = found[0]
        if key == text:
            return key, ""
        return key, "There was extra text around the key. Saved just the key."
    # Several copies run together. The last one may have other text stuck to it, so trust the first.
    if found[0] != found[1]:
        return "", "That looks like several different keys. Paste one key only. Not saved."
    return found[0], f"The key was pasted {len(found)} times. Saved one copy."


def key_problem(value: str) -> str:
    """Why a stored ANTHROPIC_API_KEY can't work, or "" if its shape is fine."""
    if not value:
        return ""
    key, _ = clean_api_key(value)
    if not key:
        return "it doesn't look like a Claude API key (they start with sk-ant-)"
    if key != value.strip():
        return "it has extra text or repeated copies in it"
    return ""


def set_env_values(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, replacing existing (or commented-out) lines for the same key."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    for index, line in enumerate(lines):
        match = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Z][A-Z0-9_]*)\s*=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def run_wizard(path: Path, ask=input, ask_secret=getpass.getpass, out=print) -> int:
    example = Path(__file__).resolve().parent.parent / ".env.example"
    if not path.exists() and example.exists():
        path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    current = read_env_values(path)
    out(f"\nHushbell setup. Answers are saved to {path} as you go. Press Enter to keep what's already there.")
    out("Keys and tokens stay hidden while you type or paste, like a password.\n")
    saved = 0
    for key, question, secret in QUESTIONS:
        existing = current.get(key, "")
        hint = " [already set]" if existing and secret else f" [{existing}]" if existing else ""
        reply = (ask_secret if secret else ask)(f"{question}{hint}: ").strip()
        rejected = False
        if reply and key == "ANTHROPIC_API_KEY":
            reply, note = clean_api_key(reply)
            rejected = not reply
            if note:
                out(f"  {note}")
        if reply:
            set_env_values(path, {key: reply})  # saved right away, so stopping halfway loses nothing
            saved += 1
            if secret:
                out(f"  saved ({len(reply)} characters)")
        elif secret and not rejected:
            out("  kept as it was" if existing else "  nothing entered, skipped")
    out(f"\nSaved {saved} setting(s). Next: `hushbell run`, then open the page it prints. "
        "`hushbell doctor` checks the setup.\n")
    return 0
