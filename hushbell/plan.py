"""Turn a sorted message into the handful of things Hushbell will do once the parent says yes.

Claude decides what a message means. This file decides what to *do* about it, with
plain rules, so the same message always gets the same plan:

    urgent pickup   -> reply to the teacher, set a "time to leave" alarm,
                       text your co-parent, open directions to school
    needs you       -> the reply or form Claude suggested, and put the date on your calendar
    later           -> nothing automatic; an "Add to calendar" button if there's a date

Every step waits for the parent's OK, and each can be unticked on the page.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from hushbell.config import Settings
from hushbell.models import Decision, Item

PICKUP_LABELS = {"fever", "vomiting", "sick", "injury", "pickup now", "early dismissal"}
CALL_APP = "Phone call"  # messages that reached Hushbell as a spoken phone call
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_PHONE = re.compile(r"^\+?[0-9]{7,15}$")


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def parse_time(value: str) -> tuple[int, int] | None:
    match = _TIME.match((value or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def spoken_time(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12 or 12
    return f"{h}:{minute:02d} {suffix}" if minute else f"{h} {suffix}"


def pretty_date(day: date, today: date) -> str:
    if day == today:
        return "today"
    if day == today + timedelta(days=1):
        return "tomorrow"
    return f"{day.strftime('%a')}, {day.strftime('%b')} {day.day}"


def valid_phone(number: str) -> bool:
    return bool(_PHONE.match(number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")))


def pretty_phone(number: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return number


def same_phone(a: str, b: str) -> bool:
    """True when two phone numbers are the same, ignoring formatting and a leading US country code."""
    da = "".join(c for c in a if c.isdigit())
    db = "".join(c for c in b if c.isdigit())
    return len(da) >= 7 and da[-10:] == db[-10:]


def short_sender(sender: str) -> str:
    return re.split(r"\s+[·\-|]\s+", sender.strip())[0] or sender.strip()


def build_steps(item: Item, decision: Decision, settings: Settings, rule_labels: list[str],
                now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    today = now.date()
    child = (decision.child or (settings.children[0] if settings.children else "")).strip()
    child = child[:1].upper() + child[1:] if child else ""
    who = child or "our kid"
    steps: list[dict] = []

    # 1. What Claude suggested: a reply in the school app, or a form to fill in.
    #    A message that came in as a phone call is answered by calling back instead.
    if item.app == CALL_APP:
        if valid_phone(item.sender):
            steps.append({"id": "main", "type": "call_back", "number": item.sender,
                          "label": f"Call {pretty_phone(item.sender)} back", "say": "connect you to them"})
    elif decision.action_type == "reply" and decision.reply_text.strip():
        name = short_sender(item.sender)
        steps.append({"id": "main", "type": "reply", "label": f"Reply to {name} in {item.app}",
                      "say": f"reply to {name}"})
    elif decision.action_type == "open_link" and decision.url.strip():
        steps.append({"id": "main", "type": "open_link", "label": "Fill in and submit the form",
                      "say": "fill in the form"})

    day = parse_date(decision.date)
    clock = parse_time(decision.time)
    pickup = decision.category == "urgent" and (
        any(label in PICKUP_LABELS for label in rule_labels) or "pick" in decision.summary.lower())

    if decision.category == "urgent":
        # 2. A "time to leave" alarm, if the message says when.
        if pickup and clock and (day is None or day == today):
            leave = datetime.combine(today, datetime.min.time()).replace(hour=clock[0], minute=clock[1])
            leave -= timedelta(minutes=max(0, settings.travel_minutes))
            if leave > now:
                steps.append({"id": "alarm", "type": "set_alarm", "time": leave.strftime("%H:%M"),
                              "message": f"Leave now to pick up {who}",
                              "label": f"Set an alarm for {spoken_time(leave.hour, leave.minute)} to leave",
                              "say": f"set an alarm for {spoken_time(leave.hour, leave.minute)}"})
        # 3. Tell the other parent.
        if settings.partner_name and valid_phone(settings.partner_phone):
            if pickup:
                when = f" by {spoken_time(*clock)}" if clock else ""
                text = f"Heads up: school says {who} needs to be picked up{when}. I'm on my way."
            else:
                text = f"Heads up from school: {decision.summary}"
            steps.append({"id": "partner", "type": "text_partner", "to": settings.partner_phone,
                          "name": settings.partner_name, "text": text[:300],
                          "label": f"Text {settings.partner_name}: “{text[:300]}”",
                          "say": f"text {settings.partner_name}"})
        # 4. Directions, ready on the phone for when you pick it up.
        if pickup and settings.school_address:
            school = settings.schools[0] if settings.schools else "school"
            steps.append({"id": "directions", "type": "directions", "destination": settings.school_address,
                          "label": f"Open directions to {school}", "say": f"open directions to {school}"})

    # 5. Put it on the calendar.
    title = decision.event_title.strip()
    if title and day and day >= today and decision.category != "urgent":
        when = pretty_date(day, today) + (f" at {spoken_time(*clock)}" if clock else "")
        steps.append({"id": "calendar", "type": "add_calendar", "title": title[:80], "date": day.isoformat(),
                      "time": f"{clock[0]:02d}:{clock[1]:02d}" if clock else "",
                      "label": f"Add “{title[:80]}” to your calendar ({when})",
                      "say": "add it to your calendar"})

    for step in steps:
        step.setdefault("enabled", True)
        step.setdefault("status", "pending")
        step.setdefault("detail", "")
    return steps


def spoken_list(steps: list[dict]) -> str:
    parts = [s["say"] for s in steps if s.get("enabled", True) and s.get("status") != "done"]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + ", and " + parts[-1]
