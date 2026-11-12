"""Fixed safety rules. They run before any AI, so a model mistake can never hide a sick child.

A match makes the item URGENT (Hushbell calls the parent). The rules lean towards
calling: a false alarm costs one phone call, a miss costs a child waiting at school.
They skip the common look-alikes ("emergency contact form", "lockdown drill").
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KID = r"(?:your (?:child|son|daughter|kid)|her|him{names})"
# The child is the object of the sentence ("we can't find Leo after recess"),
# not the owner of something ("Maya is missing her library book").
_KID_END = r"(?=\s*(?:[.,!?;]|$)|\s+(?:anywhere|after|since|on|in|at|during|from|this|today|yet|and)\b)"

_BASE_PATTERNS: list[tuple[str, str]] = [
    ("fever", r"\bfever\b|\btemperature of\b"),
    ("vomiting", r"\bvomit\w*|\bthrew up\b|\bthrowing up\b"),
    ("sick", r"\b(?:is|feels|feeling|got|been|became|looks) (?:very |really )?sick\b|\bsick (?:at|in) (?:school|class)\b"),
    ("injury", r"\binjur\w*|(?<!n't )(?<!not )(?<!never )\bhurts?\b|\bbleed\w*|\bconcussion\b|\bhead (?:injury|bump)\b"
               r"|\bbroken (?:arm|leg|bone|wrist)\b"),
    ("medical", r"\b(?:to|in|at|from) the nurse(?:'s office)?\b|\bnurse(?:'s)? office\b|\bnurse (?:called|says|said|sent|checked)\b"
                r"|\bambulance\b|\b(?:to|at|in) (?:the )?hospital\b|\ballergic reaction\b|\banaphyla\w*|\bepipen\b"
                r"|\bseizure\b|\basthma attack\b"),
    ("emergency", r"\bemergency\b(?!\s+(?:contact|card|form|info|information|drill|kit|supplies|numbers?))"
                  r"|\b911\b|\bcalled the police\b"),
    ("lockdown", r"\block ?down\b(?!\s+drill)|\bshelter[- ]in[- ]place\b(?!\s+drill)|\bevacuat\w*\b(?!\s+drill)"),
    ("early dismissal", r"\bearly (?:dismissal|release)\b|\bschool (?:is )?closing early\b|\bdismiss(?:ed|ing) early\b"),
    ("bus", r"\b(?:was not|wasn't|did not get|didn't get) (?:on|off) the bus\b|\bmissed the bus\b"),
    ("missing child", rf"\b{_KID} (?:is|was) missing{_KID_END}|\bcan(?:'|no)t (?:find|locate) {_KID}{_KID_END}"
                      rf"|\bunable to locate {_KID}{_KID_END}"),
    ("pickup now", rf"\bneeds? to be picked up\b|\bpick {_KID} up\b|\bcome (?:get|pick up) {_KID}\b"
                   rf"|\bplease (?:come|pick (?:\w+ )?up) (?:now|today|right away|immediately|asap)\b"),
]


@dataclass
class RuleHit:
    labels: list[str]

    @property
    def text(self) -> str:
        return ", ".join(self.labels)


class SafetyRules:
    def __init__(self, children: list[str] | None = None, extra_words: list[str] | None = None):
        names = "".join(f"|{re.escape(n.strip())}" for n in (children or []) if n.strip())
        patterns = [(label, rx.replace("{names}", names)) for label, rx in _BASE_PATTERNS]
        for word in extra_words or []:
            patterns.append((word.lower(), rf"\b{re.escape(word)}\b"))
        self._compiled = [(label, re.compile(rx, re.IGNORECASE)) for label, rx in patterns]

    def check(self, text: str) -> RuleHit | None:
        labels: list[str] = []
        for label, rx in self._compiled:
            if rx.search(text) and label not in labels:
                labels.append(label)
        return RuleHit(labels) if labels else None
