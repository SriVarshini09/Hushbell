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
    ("HUSHBELL_PARENT_PHONE", "Your mobile number for urgent calls, like +14155550123 (Enter to skip calls)", False),
    ("TWILIO_ACCOUNT_SID", "Twilio Account SID (Enter to skip)", False),
    ("TWILIO_AUTH_TOKEN", "Twilio Auth Token (Enter to skip)", True),
    ("TWILIO_FROM_NUMBER", "Your Twilio phone number, like +14155550100 (Enter to skip)", False),
]


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
    out(f"\nHushbell setup. Answers are saved to {path}. Press Enter to keep what's already there.\n")
    answers: dict[str, str] = {}
    for key, question, secret in QUESTIONS:
        existing = current.get(key, "")
        hint = " [already set]" if existing and secret else f" [{existing}]" if existing else ""
        reply = (ask_secret if secret else ask)(f"{question}{hint}: ").strip()
        if reply:
            answers[key] = reply
    if answers:
        set_env_values(path, answers)
    out(f"\nSaved {len(answers)} setting(s). Next: `hushbell doctor`, then `hushbell run`.\n")
    return 0
