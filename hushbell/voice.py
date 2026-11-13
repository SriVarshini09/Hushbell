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


def configure_inbound(settings: Settings, client=None) -> str:
    """Point the Twilio number's incoming calls at this Hushbell, so a caller reaches it.

    The ngrok address changes every time ngrok restarts, so this runs at start-up.
    Returns a sentence saying what happened.
    """
    if not settings.calls_enabled:
        return ""
    try:
        if client is None:
            from twilio.rest import Client

            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        found = client.incoming_phone_numbers.list(phone_number=settings.twilio_from_number, limit=1)
        if not found:
            return f"Couldn't find {settings.twilio_from_number} on your Twilio account, so incoming calls aren't set up."
        found[0].update(voice_url=f"{settings.public_base_url}/twilio/inbound", voice_method="POST")
        return f"Calls to {settings.twilio_from_number} now reach Hushbell."
    except Exception as exc:  # noqa: BLE001 - start-up must not fail over this
        return f"Couldn't set up incoming calls on Twilio: {str(exc)[:160]}"


# ---- incoming calls ---------------------------------------------------------------------

def for_voice(text: str) -> str:
    """Make a written answer sound right when read aloud."""
    lines = [ln.strip().lstrip("-•* ").strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(ln if ln.endswith((".", "!", "?")) else ln + "." for ln in lines)


def inbound_greeting_twiml(settings: Settings, attempt: int = 1) -> str:
    """Someone (the school, a coach, a judge playing the school nurse) calls Hushbell's number."""
    from twilio.twiml.voice_response import Gather, VoiceResponse

    base = settings.public_base_url
    who = f"{settings.parent_name}'s family" if settings.parent_name else "this family"
    response = VoiceResponse()
    gather = Gather(input="speech", action=f"{base}/twilio/inbound/heard", method="POST", speech_timeout="auto",
                    timeout=7, language="en-US", hints=", ".join(settings.children + settings.schools))
    if attempt == 1:
        gather.say(f"Hi, this is Hushbell, the assistant for {who}. They can't pick up right now. "
                   "Please say who you are and what this is about.")
    else:
        gather.say("Sorry, I didn't catch that. Please say who you are and what this is about.")
    response.append(gather)
    if attempt < 2:
        response.redirect(f"{base}/twilio/inbound?attempt={attempt + 1}", method="POST")
    else:
        response.say("Sorry, I still couldn't hear you. Please call again, or send a message. Goodbye.")
        response.hangup()
    return str(response)


def inbound_check_emergency_twiml(settings: Settings) -> str:
    from twilio.twiml.voice_response import Gather, VoiceResponse

    base = settings.public_base_url
    response = VoiceResponse()
    gather = Gather(num_digits=1, action=f"{base}/twilio/inbound/emergency", method="POST", timeout=6)
    gather.say("Thank you, I've got that. If this is an emergency, press 1 now. "
               "Otherwise you can hang up, and I'll pass it on.")
    response.append(gather)
    response.redirect(f"{base}/twilio/inbound/emergency?none=1", method="POST")
    return str(response)


def inbound_done_twiml(settings: Settings, urgent: bool) -> str:
    from twilio.twiml.voice_response import VoiceResponse

    name = settings.parent_name or "them"
    response = VoiceResponse()
    if urgent:
        response.say(f"Thank you. This sounds urgent, so I'm calling {name} right now. "
                     "Please keep your line free in case they call you back. Goodbye.")
    else:
        response.say(f"Thank you. I've passed that on, and {name} will see it soon. Goodbye.")
    response.hangup()
    return str(response)


def parent_ask_twiml(settings: Settings, answer: str | None = None) -> str:
    """The parent calls Hushbell from their own phone to ask about school messages."""
    from twilio.twiml.voice_response import Gather, VoiceResponse

    base = settings.public_base_url
    response = VoiceResponse()
    gather = Gather(input="speech", action=f"{base}/twilio/ask", method="POST", speech_timeout="auto", timeout=6,
                    language="en-US")
    if answer is None:
        gather.say("Hi, it's Hushbell. What would you like to know? For example: what's due this week?")
    else:
        gather.say(f"{for_voice(answer)} Anything else?")
    response.append(gather)
    response.say("Okay. Bye for now.")
    response.hangup()
    return str(response)


def make_voice(settings: Settings) -> Voice | None:
    return TwilioVoice(settings) if settings.calls_enabled else None


def _option_text(item: Item) -> str:
    from hushbell.plan import spoken_list

    if item.action_type == "open_link" and item.url:
        pass  # forms are handled below: the page only
    elif item.steps and spoken_list(item.steps):
        has_reply = any(s["type"] == "reply" for s in item.steps)
        reply = f" Your reply will say: {item.reply_text}." if has_reply and item.reply_text else ""
        return (f"Press 1 and I will {spoken_list(item.steps)}.{reply} "
                "Press 2 if you will handle it yourself.")
    if item.action_type == "reply" and item.reply_text:
        return f"Press 1 to reply: {item.reply_text}. Press 2 if you will handle it yourself."
    if item.action_type == "open_link":
        # A form link can't be checked by ear, so it is approved on the page, where the link is visible.
        return ("It comes with a form. Open your Hushbell page to see the link and approve it. "
                "Press 2 if you will handle it yourself.")
    return "Press 1 to mark this as seen. Press 2 if you will handle it yourself."


def needs_page(item: Item | None, digit: str) -> bool:
    """True when the parent pressed 1 for something that can only be approved on the page."""
    return bool(item) and digit == "1" and item.action_type == "open_link"


def call_script(item: Item) -> str:
    from hushbell.plan import CALL_APP

    news = item.summary or f"{item.sender} in {item.app} says: {item.text}"
    about = "someone just called for you" if item.app == CALL_APP else "about a school message"
    return f"Hi, this is Hushbell, {about}. {news} {_option_text(item)}"


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


def gather_twiml(digit: str, item: Item | None, base_url: str, accepted: bool, caller_id: str = "") -> str:
    """What the parent hears after pressing a key."""
    from twilio.twiml.voice_response import Dial, VoiceResponse

    response = VoiceResponse()
    call_back = next((s for s in (item.steps if item else []) if s["type"] == "call_back" and s.get("enabled", True)),
                     None)
    if item is not None and digit == "1" and accepted and call_back:
        # Connect the parent to whoever called, right now, while the other steps run on the phone.
        from hushbell.plan import spoken_list

        others = [s for s in item.steps if s["type"] != "call_back"]
        also = f" I will also {spoken_list(others)}." if others and spoken_list(others) else ""
        response.say(f"Connecting you now.{also}")
        dial = Dial(caller_id=caller_id, timeout=25) if caller_id else Dial(timeout=25)
        dial.number(call_back["number"])
        response.append(dial)
        return str(response)
    if item is None:
        response.say("Sorry, I could not find that message. Goodbye.")
    elif digit not in ("1", "2"):
        response.redirect(f"{base_url}/twilio/voice/{item.id}?attempt=2", method="POST")
        return str(response)
    elif needs_page(item, digit):
        response.say("Forms are approved on your Hushbell page, where you can see the link. "
                     "It is waiting for you there. Goodbye.")
    elif not accepted:
        response.say("This message was already handled. Goodbye.")
    elif digit == "2":
        response.say("Okay, you will handle it. I will not do anything. Goodbye.")
    elif item.steps:
        from hushbell.plan import spoken_list

        response.say(f"Done. I am on it: I will {spoken_list(item.steps)}. "
                     "You can watch it happen on your Hushbell page. Goodbye.")
    elif item.action_type == "reply":
        response.say("Done. I am sending your reply now. Goodbye.")
    else:
        response.say("Marked as seen. Goodbye.")
    response.hangup()
    return str(response)
