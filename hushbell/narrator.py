"""A natural voice for Hushbell's calls, from ElevenLabs.

Every <Say> in a call becomes a <Play> of the same words spoken by an ElevenLabs voice. The
clips are made once, kept in the data folder, and served to Twilio at /voice/<id>.mp3.
If ElevenLabs is not set up or doesn't answer in time, the call keeps Twilio's own voice,
so a call is never lost over a voice.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from hushbell.config import Settings

log = logging.getLogger(__name__)

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice}"
SAY = re.compile(r"<Say(?:\s[^>]*)?>(.*?)</Say>", re.S)
CLIP_ID = re.compile(r"^[0-9a-f]{32}$")


class Narrator:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self.folder = settings.data_dir / "voice"
        self.http = http
        self._warned = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.elevenlabs_api_key and self.settings.public_base_url)

    def clip_id(self, text: str) -> str:
        """A name for the clip that nobody can guess from the words (it's keyed with the access token)."""
        material = f"{self.settings.elevenlabs_voice}|{self.settings.elevenlabs_model}|{text}".encode()
        return hmac.new(self.settings.access_token.encode(), material, hashlib.sha256).hexdigest()[:32]

    def path(self, clip: str) -> Path | None:
        if not CLIP_ID.match(clip):
            return None
        candidate = self.folder / f"{clip}.mp3"
        return candidate if candidate.exists() else None

    def clip(self, text: str) -> str | None:
        """Make (or reuse) the clip for these words. Returns its id, or None to keep Twilio's voice."""
        clip = self.clip_id(text)
        target = self.folder / f"{clip}.mp3"
        if target.exists():
            return clip
        try:
            client = self.http or httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0))
            response = client.post(
                API.format(voice=self.settings.elevenlabs_voice),
                params={"output_format": "mp3_22050_32"},
                headers={"xi-api-key": self.settings.elevenlabs_api_key, "Accept": "audio/mpeg"},
                json={"text": text, "model_id": self.settings.elevenlabs_model},
            )
            if response.status_code != 200 or not response.content:
                raise RuntimeError(f"ElevenLabs answered {response.status_code}: {response.text[:120]}")
            self.folder.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".part")
            partial.write_bytes(response.content)
            partial.replace(target)
            return clip
        except Exception as exc:  # noqa: BLE001 - fall back to Twilio's voice, never drop the call
            if not self._warned:
                log.warning("ElevenLabs voice unavailable, using Twilio's voice: %s", exc)
                self._warned = True
            return None

    def narrate(self, twiml: str) -> str:
        """Swap each <Say> for an ElevenLabs clip (made in parallel)."""
        if not self.enabled:
            return twiml
        lines = [html.unescape(m.group(1)) for m in SAY.finditer(twiml)]
        if not lines:
            return twiml
        with ThreadPoolExecutor(max_workers=4) as pool:
            clips = dict(zip(lines, pool.map(self.clip, lines)))
        base = self.settings.public_base_url

        def swap(match: re.Match) -> str:
            clip = clips.get(html.unescape(match.group(1)))
            return f"<Play>{base}/voice/{clip}.mp3</Play>" if clip else match.group(0)

        return SAY.sub(swap, twiml)
