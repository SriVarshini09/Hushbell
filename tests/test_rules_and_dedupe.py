import pytest

from hushbell.triage.dedupe import find_duplicate, overlap
from hushbell.triage.rules import SafetyRules
from hushbell.models import Item

rules = SafetyRules(children=["Maya", "Leo"])


@pytest.mark.parametrize("text,label", [
    ("Maya has a fever (101°F). Please pick her up by 1pm.", "fever"),
    ("Leo threw up after lunch", "vomiting"),
    ("Maya bumped her head and is in the nurse's office", "medical"),
    ("The school is in lockdown. Do not come to campus.", "lockdown"),
    ("Early dismissal today at 12:30 due to a power outage", "early dismissal"),
    ("Leo did not get on the bus this afternoon", "bus"),
    ("Can you come get her? She is not feeling well and needs to be picked up", "pickup now"),
    ("Please pick Maya up at the front office", "pickup now"),
    ("We are unable to locate Leo after recess", "missing child"),
])
def test_urgent_messages_are_caught(text, label):
    hit = rules.check(text)
    assert hit is not None and label in hit.labels


@pytest.mark.parametrize("text", [
    "Bake sale Friday! Bring cookies.",
    "Please update your emergency contact form by Friday",
    "Lockdown drill tomorrow at 10am, nothing to worry about",
    "Pick up your yearbook order at the library",
    "Maya is missing her library book, please look at home",
    "Maya earned a point for teamwork",
    "We can't find the field trip forms, please resend",
    "Picture day is Thursday",
    "It doesn't hurt to ask about volunteer spots!",
    "We can't find her library book, could you check at home?",
])
def test_everyday_messages_are_not_urgent(text):
    assert rules.check(text) is None


def test_extra_words_from_settings():
    custom = SafetyRules(extra_words=["head lice"])
    assert custom.check("A case of head lice in Room 12").labels == ["head lice"]


def _item(text):
    return Item(id="x", created_at=0, updated_at=0, app="Remind", sender="t", text=text)


def test_same_news_from_two_apps_is_merged():
    remind = _item("Field trip form due TODAY: forms.gle/fieldtrip12")
    assert overlap("Jen: Reminder: field trip form due today!", remind.text) >= 0.7
    assert find_duplicate("Jen: Reminder: field trip form due today!", [remind]) is remind


def test_different_news_is_not_merged():
    assert find_duplicate("Bake sale Friday! Bring cookies.", [_item("Field trip form due TODAY")]) is None
    assert find_duplicate("Thanks!", [_item("Thanks Jen!")]) is None
