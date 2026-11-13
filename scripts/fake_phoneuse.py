"""A stand-in for PhoneUse, for rehearsing cloud-phone mode without a PhoneUse account.

It answers the handful of PhoneUse calls Hushbell makes (devices, screenshot, lease, gestures,
tasks) on your own computer, and draws an Android-style notification shade for Hushbell to read.
Nothing here talks to PhoneUse, and it proves nothing about the real service.

    pip install pillow                       # only this script needs it
    python scripts/fake_phoneuse.py          # listens on http://127.0.0.1:8799

    # in another terminal (PowerShell: use $env:NAME="value" lines instead)
    HUSHBELL_PHONE_BACKEND=phoneuse PHONEUSE_API_KEY=pbk_fake \
    HUSHBELL_PHONEUSE_URL=http://127.0.0.1:8799 hushbell run --demo

Then put messages on the pretend phone, and choose how its next task ends:

    python scripts/fake_phoneuse.py notify Remind "Ms. Rivera" "Can Anna stay for chess club Wednesday until 4:30?"
    python scripts/fake_phoneuse.py next needs_user_control     # or: succeeded, failed, slow
    python scripts/fake_phoneuse.py clear
    python scripts/fake_phoneuse.py log                         # the tasks Hushbell handed over
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import textwrap
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEVICE = "fake-cloud-1"
OUTCOMES = {
    "succeeded": ["running", "succeeded"],
    "needs_user_control": ["running", "needs_user_control"],
    "failed": ["running", "failed"],
    "slow": ["running", "running", "running", "running", "succeeded"],
}


class Phone:
    """The pretend cloud phone: what is in its shade, and how its tasks go."""

    def __init__(self, step_seconds: float):
        self.lock = threading.Lock()
        self.notifications: list[tuple[str, str, str]] = []     # (app, title, text), newest first
        self.shade_open = False
        self.next_outcomes: list[str] = []
        self.runs: dict[str, dict] = {}
        self.goals: list[str] = []
        self.step_seconds = step_seconds

    def screenshot(self) -> bytes:
        with self.lock:
            return draw_shade(self.notifications) if self.shade_open else draw_home()


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    for name in (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
                 else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _jpeg(image) -> bytes:
    out = io.BytesIO()
    image.save(out, "JPEG", quality=88)
    return out.getvalue()


def draw_home() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 1280), "#20324f")
    draw = ImageDraw.Draw(image)
    draw.text((26, 14), "10:42", font=_font(22), fill="#e6e1e5")
    for i, color in enumerate(["#5b8def", "#46c38b", "#25d366", "#ea4335"]):
        draw.rounded_rectangle((60 + i * 160, 1040, 160 + i * 160, 1140), 26, fill=color)
    return _jpeg(image)


def draw_shade(notifications: list[tuple[str, str, str]]) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (720, 1280), "#1b1b1f")
    draw = ImageDraw.Draw(image)
    draw.text((26, 14), "10:42", font=_font(22), fill="#e6e1e5")
    draw.text((600, 14), "5G  87%", font=_font(22), fill="#e6e1e5")
    for i, color in enumerate(["#a8c7fa", "#3c3c44", "#3c3c44", "#a8c7fa"]):
        draw.rounded_rectangle((22 + i * 172, 62, 22 + i * 172 + 158, 126), 32, fill=color)
    y = 150
    if not notifications:
        draw.text((250, y + 40), "No notifications", font=_font(24), fill="#cac4d0")
    for app, title, text in notifications:
        lines = textwrap.wrap(text, 56) or [""]
        height = 96 + 32 * len(lines)
        if y + height > 1260:
            break
        draw.rounded_rectangle((18, y, 702, y + height), 26, fill="#2b2930")
        draw.text((42, y + 18), f"{app} · now", font=_font(18), fill="#cac4d0")
        draw.text((42, y + 46), title, font=_font(24, bold=True), fill="#e6e1e5")
        for n, line in enumerate(lines):
            draw.text((42, y + 82 + 32 * n), line, font=_font(22), fill="#e6e1e5")
        y += height + 12
    return _jpeg(image)


def make_handler(phone: Phone):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # one tidy line per interesting request instead
            pass

        def _send(self, status: int, body=None, content_type: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body if body is not None else {}).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

        def _handle(self) -> None:
            url = urlsplit(self.path)
            path, method = url.path, self.command
            body = self._body() if method in ("POST", "PATCH", "DELETE") else {}

            # ---- the rehearsal's own controls (not part of PhoneUse) ----
            if path == "/fake/notify":
                with phone.lock:
                    phone.notifications.insert(0, (str(body.get("app", "Remind")), str(body.get("title", "")),
                                                   str(body.get("text", ""))))
                print(f"  shade: + {body.get('app')} · {body.get('title')}: {body.get('text')}")
                return self._send(200, {"ok": True, "notifications": len(phone.notifications)})
            if path == "/fake/clear":
                with phone.lock:
                    phone.notifications.clear()
                print("  shade: cleared")
                return self._send(200, {"ok": True})
            if path == "/fake/next":
                outcome = str(body.get("outcome", "succeeded"))
                if outcome not in OUTCOMES:
                    return self._send(400, {"error": {"code": "bad_outcome", "message": f"one of {sorted(OUTCOMES)}"}})
                phone.next_outcomes.append(outcome)
                print(f"  next task will end: {outcome}")
                return self._send(200, {"ok": True})
            if path == "/fake/log":
                return self._send(200, {"goals": phone.goals, "shade_open": phone.shade_open,
                                        "notifications": phone.notifications})

            # ---- PhoneUse's API, as Hushbell uses it ----
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"error": {"code": "unauthorized", "message": "Missing API key"}})
            if path == "/mcp":
                call = body.get("params", {})
                if call.get("name") == "list_devices":
                    result = {"devices": [{"deviceId": DEVICE, "kind": "cloud_phone", "status": "online"}]}
                else:
                    result = {"accepted": True}
                return self._send(200, {"jsonrpc": "2.0", "id": body.get("id", 1),
                                        "result": {"structuredContent": result}})
            if path.endswith("/screenshot"):
                return self._send(200, phone.screenshot(), "image/jpeg")
            if path.endswith("/lease"):
                if method == "DELETE":
                    return self._send(200, {})
                return self._send(201 if method == "POST" else 200, {"leaseId": "lease-fake"})
            if path.endswith("/actions"):
                if body.get("action") == "swipe":
                    phone.shade_open = True
                elif body.get("action") == "press_key":
                    phone.shade_open = False
                return self._send(200, {"status": "committed"})
            if path == "/v1/runs" and method == "POST":
                run_id = f"task_{len(phone.runs) + 1}"
                outcome = phone.next_outcomes.pop(0) if phone.next_outcomes else "succeeded"
                phone.runs[run_id] = {"states": list(OUTCOMES[outcome]), "goal": body.get("goal", "")}
                phone.goals.append(body.get("goal", ""))
                print(f"  task {run_id} ({outcome}): {body.get('goal', '')[:110]}")
                return self._send(201, {"runId": run_id, "state": "queued", "executor": "phoneuse"})
            if path.startswith("/v1/runs/") and path.endswith("/cancel"):
                return self._send(200, {})
            if path.startswith("/v1/runs/") and method == "GET":
                run_id = path.rsplit("/", 1)[1]
                run = phone.runs.get(run_id)
                if run is None:
                    return self._send(404, {"error": {"code": "not_found", "message": "No such task"}})
                time.sleep(phone.step_seconds)          # a real agent takes a while per step
                state = run["states"].pop(0) if len(run["states"]) > 1 else run["states"][0]
                answer = {"runId": run_id, "state": state}
                if state == "succeeded":
                    answer["detail"] = "Done on the cloud phone."
                if state == "failed":
                    answer["reason"] = "budget_exhausted"
                return self._send(200, answer)
            return self._send(404, {"error": {"code": "not_found", "message": path}})

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

    return Handler


def control(port: int, path: str, body: dict | None = None) -> int:
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body or {}).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            print(json.dumps(json.load(response), indent=1, ensure_ascii=False))
        return 0
    except OSError as exc:
        print(f"Couldn't reach the pretend PhoneUse on port {port}: {exc}. Is it running?")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A pretend PhoneUse for rehearsing Hushbell's cloud-phone mode.")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--step-seconds", type=float, default=1.5, help="how long each task step takes")
    sub = parser.add_subparsers(dest="command")
    notify = sub.add_parser("notify", help="put a notification on the pretend phone")
    notify.add_argument("app")
    notify.add_argument("title")
    notify.add_argument("text")
    nxt = sub.add_parser("next", help="how the next task ends")
    nxt.add_argument("outcome", choices=sorted(OUTCOMES))
    sub.add_parser("clear", help="clear the pretend phone's notifications")
    sub.add_parser("log", help="show the tasks Hushbell handed over")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace", line_buffering=True)

    if args.command == "notify":
        return control(args.port, "/fake/notify", {"app": args.app, "title": args.title, "text": args.text})
    if args.command == "next":
        return control(args.port, "/fake/next", {"outcome": args.outcome})
    if args.command in ("clear", "log"):
        return control(args.port, f"/fake/{args.command}")

    try:
        import PIL  # noqa: F401
    except ImportError:
        print("This script draws the pretend phone's screen with Pillow. Run: pip install pillow")
        return 1
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(Phone(args.step_seconds)))
    print(f"Pretend PhoneUse on http://127.0.0.1:{args.port} (a rehearsal stand-in, not the real service).")
    print(f"Start Hushbell with HUSHBELL_PHONE_BACKEND=phoneuse PHONEUSE_API_KEY=pbk_fake "
          f"HUSHBELL_PHONEUSE_URL=http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
