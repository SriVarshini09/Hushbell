"""Shared data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

Category = Literal["urgent", "needs_you", "later"]
ActionType = Literal["none", "reply", "open_link"]

# Item status values, in roughly the order an item moves through them.
STATUS_NEW = "new"
STATUS_SORTING = "sorting"
STATUS_CALLING = "calling"      # urgent: Hushbell is phoning the parent
STATUS_WAITING = "waiting"      # waiting for the parent's OK (call keypress or page)
STATUS_ACTING = "acting"        # the phone agent is doing the task
STATUS_DONE = "done"
STATUS_SKIPPED = "skipped"      # the parent said no / will handle it
STATUS_FAILED = "failed"
STATUS_LATER = "later"          # filed for the digest

OPEN_STATUSES = {STATUS_CALLING, STATUS_WAITING}


@dataclass
class IncomingMessage:
    """A school message as it arrived on the phone (or through the ingest API)."""

    app: str                     # label, e.g. "Remind"
    sender: str                  # notification title: teacher, group, or sender name
    text: str
    package: str = ""
    key: str = ""                # Android notification key, when read from the phone
    source: str = "phone"        # "phone", "api", "demo"


class Decision(BaseModel):
    """How Hushbell sorted one item."""

    category: Category
    reason: str
    summary: str
    child: str = ""
    due: str = ""
    action_type: ActionType = "none"
    reply_text: str = ""
    url: str = ""
    instructions: str = ""
    rule_hit: str = ""           # safety words that matched, if any
    sorted_by: str = "claude"    # "claude", "rules", or "fallback"


@dataclass
class Item:
    id: str
    created_at: float
    updated_at: float
    app: str
    sender: str
    text: str
    status: str = STATUS_NEW
    category: str = ""
    reason: str = ""
    summary: str = ""
    child: str = ""
    due: str = ""
    action_type: str = "none"
    reply_text: str = ""
    url: str = ""
    instructions: str = ""
    rule_hit: str = ""
    sorted_by: str = ""
    result: str = ""
    sources: list[str] = field(default_factory=list)
    duplicate_count: int = 0
    call_attempts: int = 0

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "app": self.app,
            "sender": self.sender,
            "text": self.text,
            "status": self.status,
            "category": self.category,
            "reason": self.reason,
            "summary": self.summary,
            "child": self.child,
            "due": self.due,
            "action_type": self.action_type,
            "reply_text": self.reply_text,
            "url": self.url,
            "instructions": self.instructions,
            "rule_hit": self.rule_hit,
            "sorted_by": self.sorted_by,
            "result": self.result,
            "sources": self.sources,
            "duplicate_count": self.duplicate_count,
        }
