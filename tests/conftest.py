from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from hushbell.config import DEFAULT_APPS, Settings
from hushbell.engine import Engine, InlineExecutor
from hushbell.models import Decision
from hushbell.phone import DryRunPhone
from hushbell.store import Store
from hushbell.triage.classifier import ClassifierError


class FakeClassifier:
    """Sorts by keyword so tests don't call Claude."""

    def __init__(self):
        self.fail = False
        self.calls = []

    def classify(self, app, sender, text, also_seen_in=None):
        self.calls.append((app, sender, text, also_seen_in))
        if self.fail == "crash":
            raise TypeError("Could not resolve authentication method")
        if self.fail:
            raise ClassifierError("offline")
        low = text.lower()
        if "form" in low:
            return Decision(category="needs_you", reason="A parent must sign today",
                            summary="Field trip permission form is due today.", due="today",
                            action_type="open_link", url="https://forms.gle/fieldtrip12",
                            instructions="Give permission for the trip.")
        if "javascript" in low:
            return Decision(category="needs_you", reason="Link", summary="Odd link", action_type="open_link",
                            url="javascript:alert(1)")
        # Deliberately calls the fever message "later": the safety rules must still catch it.
        return Decision(category="later", reason="General update", summary=text[:60])


class FakeVoice:
    def __init__(self):
        self.calls: list[str] = []
        self.fail = False

    def place_call(self, item_id: str) -> str:
        if self.fail:
            raise RuntimeError("twilio down")
        self.calls.append(item_id)
        return f"CA{len(self.calls)}"

    def place_test_call(self) -> str:
        if self.fail:
            raise RuntimeError("twilio down")
        self.calls.append("test")
        return "CAtest"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        data_dir=tmp_path, access_token="test-token", children=["Maya"], parent_name="Ana Lopez",
        schools=["Lincoln Elementary"], keywords=["3rd grade parents", "Lincoln Elementary"],
        apps=dict(DEFAULT_APPS), phone_backend="dry-run", redial_seconds=0,
        public_base_url="https://hushbell.example", twilio_validate=False,
    )


@pytest.fixture
def make_engine(settings):
    def _make(voice=None):
        store = Store(":memory:")
        return Engine(settings=settings, store=store, classifier=FakeClassifier(), phone=DryRunPhone(settings),
                      voice=voice, checkpointer=InMemorySaver(), executor=InlineExecutor())
    return _make
