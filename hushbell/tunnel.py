"""Find the public address of a running ngrok tunnel, so Twilio can reach Hushbell."""

from __future__ import annotations

import json
import urllib.request

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def detect_ngrok(port: int, opener=urllib.request.urlopen) -> str:
    """Return the https address ngrok is forwarding to `port`, or "" if there isn't one."""
    try:
        with opener(NGROK_API, timeout=1.5) as response:
            data = json.load(response)
    except Exception:  # noqa: BLE001 - ngrok not running is the normal case
        return ""
    https = [t for t in data.get("tunnels", []) if str(t.get("public_url", "")).startswith("https://")]
    for tunnel in https:
        address = str((tunnel.get("config") or {}).get("addr", ""))
        if address.endswith(f":{port}"):
            return str(tunnel["public_url"]).rstrip("/")
    return str(https[0]["public_url"]).rstrip("/") if len(https) == 1 else ""
