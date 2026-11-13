"""Find Hushbell's addresses: a public one Twilio can reach (Replit or ngrok), and the local one."""

from __future__ import annotations

import json
import os
import socket
import urllib.request

NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def detect_replit(env=os.environ) -> str:
    """The https address of this Replit app (a deployment's domain, or the workspace preview)."""
    domains = [d.strip() for d in env.get("REPLIT_DOMAINS", "").split(",") if d.strip()]
    domain = domains[0] if domains else env.get("REPLIT_DEV_DOMAIN", "").strip()
    return f"https://{domain}" if domain else ""


def lan_ip() -> str:
    """This computer's address on the local network, for opening the page from a phone."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))  # no packet is sent; this only picks the outgoing interface
            return probe.getsockname()[0]
    except OSError:
        return ""


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
