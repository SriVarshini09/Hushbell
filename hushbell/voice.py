"""Calling the parent through Do Not Disturb (Twilio).

Save Hushbell's Twilio number on the parent's phone as a starred contact and allow
starred contacts in Do Not Disturb. Then Hushbell is the only caller that rings.
If the first call is missed, Hushbell calls once more: most phones also let a
second call from the same number within a few minutes ring through.
"""

from __future__ import annotations

import logging
from typing import Protocol

from hushbell.config import Settings
from hushbell.models import Item

log = logging.getLogger(__name__)


class Voice(Protocol):
    def place_call(self, item_id: str) -> str: ...


class TwilioVoice:
    def __init__(self, settings: Settings):
        from twilio.rest import Client

        self.settings = settings
        self.client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

    def place_call(self, item_id: str) -> str:
        base = self.settings.public_base_url
        call = self.client.calls.create(
            to=self.settings.parent_phone,
            from_=self.settings.twilio_from_number,
            url=f"{base}/twilio/voice/{item_id}",
            method="POST",
            status_callback=f"{base}/twilio/status/{item_id}",
            status_callback_method="POST",
        )
        return call.sid

    def place_test_call(self) -> str:
        """Ring the parent with a short test message, to check it gets through Do Not Disturb."""
        call = self.client.calls.create(
            to=self.settings.parent_phone,
            from_=self.settings.twilio_from_number,
            url=f"{self.settings.public_base_url}/twilio/test",
            method="POST",
        )
        return call.sid


def ring_test_twiml() -> str:
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    response.say("This is a test call from Hushbell. If your phone rang while Do Not Disturb was on, "
                 "you are all set. Goodbye.")
    response.hangup()
    return str(response)


def make_voice(settings: Settings) -> Voice | None:
    return TwilioVoice(settings) if settings.calls_enabled else None


def _option_text(item: Item) -> str:
    if item.action_type == "reply" and item.reply_text:
        return f"Press 1 to reply: {item.reply_text}. Press 2 if you will handle it yourself."
    if item.action_type == "open_link":
        return "Press 1 and I will fill in the form for you. Press 2 if you will handle it yourself."
    return "Press 1 to mark this as seen. Press 2 if you will handle it yourself."


def call_script(item: Item) -> str:
    news = item.summary or f"{item.sender} in {item.app} says: {item.text}"
    return f"Hi, this is Hushbell, about a school message. {news} {_option_text(item)}"


def voice_twiml(item: Item | None, base_url: str, attempt: int = 1) -> str:
    """The first thing the parent hears when they pick up."""
    from twilio.twiml.voice_response import Gather, VoiceResponse

    response = VoiceResponse()
    if item is None:
        response.say("Sorry, I could not find that message. Please check your Hushbell page. Goodbye.")
        response.hangup()
        return str(response)
    gather = Gather(num_digits=1, action=f"{base_url}/twilio/gather/{item.id}", method="POST", timeout=8)
    gather.say(call_script(item))  # the TwiML library escapes the text
    response.append(gather)
    if attempt < 2:
        response.redirect(f"{base_url}/twilio/voice/{item.id}?attempt={attempt + 1}", method="POST")
    else:
        response.say("Okay. It is waiting for you on your Hushbell page. Goodbye.")
        response.hangup()
    return str(response)


def gather_twiml(digit: str, item: Item | None, base_url: str, accepted: bool) -> str:
    """What the parent hears after pressing a key."""
    from twilio.twiml.voice_response import VoiceResponse

    response = VoiceResponse()
    if item is None:
        response.say("Sorry, I could not find that message. Goodbye.")
    elif digit not in ("1", "2"):
        response.redirect(f"{base_url}/twilio/voice/{item.id}?attempt=2", method="POST")
        return str(response)
    elif not accepted:
        response.say("This message was already handled. Goodbye.")
    elif digit == "2":
        response.say("Okay, you will handle it. I will not do anything. Goodbye.")
    elif item.action_type == "reply":
        response.say("Done. I am sending your reply now. Goodbye.")
    elif item.action_type == "open_link":
        response.say("Done. I am filling in the form now. Goodbye.")
    else:
        response.say("Marked as seen. Goodbye.")
    response.hangup()
    return str(response)
