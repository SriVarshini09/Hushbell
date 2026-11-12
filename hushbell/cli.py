"""Command line: `hushbell run`, `hushbell doctor`, `hushbell notifications`, `hushbell send`, `hushbell token`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from hushbell import __version__
from hushbell.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hushbell", description="Hushed all day. Rings when your kid needs you.")
    parser.add_argument("--version", action="version", version=f"hushbell {__version__}")
    parser.add_argument("--env", help="path to a .env file (default: ./.env)")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start Hushbell: read the phone, sort messages, serve the page")
    run.add_argument("--no-phone", action="store_true", help="don't read the phone (use the page's demo buttons or `hushbell send`)")
    run.add_argument("--dry-run", action="store_true", help="never act on the phone; only log what would happen")
    run.add_argument("--demo", action="store_true", help="show demo message buttons on the page")
    run.add_argument("--host")
    run.add_argument("--port", type=int)

    doctor = sub.add_parser("doctor", help="check the setup and say what to fix")
    doctor.add_argument("--live", action="store_true", help="also make one small Claude call and one Twilio lookup")

    notes = sub.add_parser("notifications", help="show the phone's notifications and which ones Hushbell would read")
    notes.add_argument("--raw", action="store_true", help="print the phone's raw output (for bug reports)")

    init = sub.add_parser("init", help="answer a few questions and save them to .env")
    init.add_argument("--file", default=".env", help="where to save (default: ./.env)")

    sub.add_parser("call-test", help="ring your phone once to check the call gets through Do Not Disturb "
                                     "(Hushbell must be running)")

    send = sub.add_parser("send", help="send a test school message to a running Hushbell")
    send.add_argument("text")
    send.add_argument("--app", default="Remind")
    send.add_argument("--sender", default="Ms. Rivera · Room 12")

    sub.add_parser("token", help="print the access token for the Hushbell page")

    args = parser.parse_args(argv)
    if args.command == "init":
        from pathlib import Path

        from hushbell.setup_wizard import run_wizard
        return run_wizard(Path(args.file))
    settings = Settings.from_env(args.env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "run":
        return cmd_run(settings, args)
    if args.command == "doctor":
        return cmd_doctor(settings, args.live)
    if args.command == "notifications":
        return cmd_notifications(settings, args.raw)
    if args.command == "call-test":
        return cmd_call_test(settings)
    if args.command == "send":
        return cmd_send(settings, args)
    if args.command == "token":
        print(settings.access_token)
        return 0
    return 1


def cmd_run(settings: Settings, args) -> int:
    import uvicorn

    from hushbell.ingest.adb import AdbNotificationSource
    from hushbell.runtime import DigestClock, PhoneReader, build_engine
    from hushbell.web.app import create_app

    if args.no_phone:
        settings.read_phone = False
    if args.dry_run:
        settings.phone_backend = "dry-run"
    if args.demo:
        settings.demo_mode = True
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    _use_ngrok_if_running(settings)
    engine = build_engine(settings)
    app = create_app(engine)
    recovered = engine.recover()
    if recovered:
        print(f"  Picked up {recovered} message(s) that were in progress when Hushbell last stopped.")

    reader = None
    if settings.read_phone:
        source = AdbNotificationSource(settings, store=engine.store)
        if source.available():
            reader = PhoneReader(engine, source)
            reader.start()
        else:
            print(f"[!!] adb not found ('{settings.adb_path}'). Not reading the phone. Run `hushbell doctor`.")
    clock = DigestClock(engine)
    clock.start()

    print()
    print("  HUSHBELL is running.")
    print(f"  Page:   http://localhost:{settings.port}   (on your phone: http://<this laptop's IP>:{settings.port})")
    print(f"  Token:  {settings.access_token}")
    print(f"  Calls:  {'on' if settings.calls_enabled else 'off (set TWILIO_* and HUSHBELL_PARENT_PHONE)'}")
    print(f"  Phone:  reading {'on' if reader else 'off'} · actions: {engine.phone.name}")
    print(f"  Data:   {settings.data_dir}")
    print()
    try:
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    finally:
        if reader:
            reader.stop_event.set()
        clock.stop_event.set()
        engine.shutdown()
    return 0


def _use_ngrok_if_running(settings: Settings) -> None:
    """If no public address is set but ngrok is forwarding to our port, use its address."""
    if settings.public_base_url:
        return
    from hushbell.tunnel import detect_ngrok

    found = detect_ngrok(settings.port)
    if found:
        settings.public_base_url = found
        print(f"  Using your ngrok address for calls: {found}")


def cmd_call_test(settings: Settings) -> int:
    _use_ngrok_if_running(settings)
    if not settings.calls_enabled:
        print("Calls are not set up. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER and "
              "HUSHBELL_PARENT_PHONE, and start ngrok (`ngrok http 8000`) or set HUSHBELL_PUBLIC_URL.")
        return 1
    from hushbell.voice import TwilioVoice

    try:
        TwilioVoice(settings).place_test_call()
    except Exception as exc:  # noqa: BLE001
        print(f"The test call could not be placed: {exc}")
        return 1
    print(f"Calling {settings.parent_phone} now. Turn on Do Not Disturb first: if it still rings, you're set.")
    print("If it doesn't ring, star the Hushbell contact and allow starred contacts in Do Not Disturb.")
    return 0


def _ok(msg: str) -> None:
    print(f"  [ok] {msg}")


def _warn(msg: str, fix: str = "") -> None:
    print(f"  [!!] {msg}")
    if fix:
        print(f"       fix: {fix}")


def cmd_doctor(settings: Settings, live: bool) -> int:
    problems = 0
    print(f"\nHushbell {__version__} setup check\n")

    print("Basics")
    if sys.version_info >= (3, 11):
        _ok(f"Python {sys.version.split()[0]}")
    else:
        problems += 1
        _warn(f"Python {sys.version.split()[0]} is too old", "install Python 3.11-3.13")
    _ok(f"data folder: {settings.data_dir}")
    _ok("access token is set (see `hushbell token`)")

    print("\nClaude (sorting)")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        _ok(f"credentials found; model {settings.model}, effort {settings.effort}")
    else:
        problems += 1
        _warn("ANTHROPIC_API_KEY is not set, so messages can't be sorted (they will all wait for you)",
              "run `hushbell init`, or add the key to .env")
    if live:
        from hushbell.triage.classifier import ClaudeClassifier, ClassifierError
        try:
            d = ClaudeClassifier(settings).classify("Remind", "Ms. Rivera", "Bake sale Friday! Bring cookies.")
            _ok(f"live test sorted a bake-sale message as '{d.category}'")
        except ClassifierError as exc:
            problems += 1
            _warn(f"live Claude test failed: {exc}")

    print("\nPhone (reading notifications)")
    if not shutil.which(settings.adb_path):
        problems += 1
        _warn(f"adb not found ('{settings.adb_path}')",
              "install Android platform-tools and add adb to PATH, or set HUSHBELL_ADB")
    else:
        out = subprocess.run([settings.adb_path, "devices"], capture_output=True, text=True).stdout
        devices = [ln.split()[0] for ln in out.splitlines()[1:] if ln.strip().endswith("device")]
        if not devices:
            problems += 1
            _warn("no phone connected", "plug in the Android, turn on USB debugging, accept the prompt on the phone")
        else:
            _ok(f"phone connected: {', '.join(devices)}")
            problems += _check_notifications(settings)
    mixed = [label for _, (label, kind) in settings.apps.items() if kind == "mixed"]
    if mixed and not settings.keywords:
        _warn(f"{', '.join(mixed)} will be ignored: no HUSHBELL_KEYWORDS set",
              "set HUSHBELL_KEYWORDS to your school name and parent-group name")

    print("\nPhone (acting after your OK)")
    if settings.phone_backend == "dry-run":
        _ok("dry run: Hushbell will only log what it would do")
    else:
        from hushbell.phone import MobilerunPhone
        if MobilerunPhone(settings).available():
            _ok(f"Mobilerun found ({settings.mobilerun_cmd}). Run its `setup` and `ping` once to prepare the phone")
        else:
            problems += 1
            _warn("Mobilerun not found",
                  'uv tool install "mobilerun[anthropic]"  then  mobilerun setup  and  mobilerun ping')

    print("\nCalls through Do Not Disturb (Twilio)")
    _use_ngrok_if_running(settings)
    if settings.calls_enabled:
        _ok(f"calling {settings.parent_phone} from {settings.twilio_from_number}")
        if not settings.public_base_url.startswith("https://"):
            _warn("HUSHBELL_PUBLIC_URL should be an https address Twilio can reach", "ngrok http 8000")
        if live:
            try:
                from twilio.rest import Client
                found = Client(settings.twilio_account_sid, settings.twilio_auth_token).incoming_phone_numbers.list(
                    phone_number=settings.twilio_from_number, limit=1)
                if found:
                    _ok("Twilio number belongs to this account")
                else:
                    problems += 1
                    _warn("TWILIO_FROM_NUMBER is not on this Twilio account")
            except Exception as exc:  # noqa: BLE001
                problems += 1
                _warn(f"Twilio check failed: {exc}")
        print("       remember: save this number on the phone as a starred contact and allow")
        print("       starred contacts in Do Not Disturb, so Hushbell's call rings through.")
    else:
        _warn("calls are off; urgent messages will wait at the top of the page",
              "set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, HUSHBELL_PARENT_PHONE, HUSHBELL_PUBLIC_URL")

    print(f"\n{'All set.' if problems == 0 else f'{problems} thing(s) to fix.'}\n")
    return 0 if problems == 0 else 1


def _check_notifications(settings: Settings) -> int:
    from hushbell.ingest.adb import AdbNotificationSource, parse_dumpsys, to_message
    try:
        records = parse_dumpsys(AdbNotificationSource(settings).read_raw())
    except Exception as exc:  # noqa: BLE001
        _warn(f"could not read notifications: {exc}")
        return 1
    readable = [r for r in records if to_message(r, settings)]
    packages = sorted({r.package for r in records})
    _ok(f"read {len(records)} notifications; {len(readable)} look like school messages")
    unknown = [p for p in packages if p not in settings.apps]
    if unknown:
        print(f"       other apps posting now: {', '.join(unknown[:12])}")
        print("       (add a school app with HUSHBELL_APPS=package=Label:school)")
    return 0


def cmd_notifications(settings: Settings, raw: bool = False) -> int:
    from hushbell.ingest.adb import AdbNotificationSource, parse_dumpsys, to_message
    try:
        output = AdbNotificationSource(settings).read_raw()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the phone: {exc}")
        return 1
    if raw:
        sys.stdout.write(output)
        return 0
    records = parse_dumpsys(output)
    if not records:
        print("No notifications on the phone right now.")
    for r in records:
        msg = to_message(r, settings)
        verdict = "READ" if msg else "skip"
        label = settings.apps.get(r.package, ("-", ""))[0]
        print(f"[{verdict}] {r.package} ({label}) | {r.title[:40]} | {r.body[:70]}")
    return 0


def cmd_send(settings: Settings, args) -> int:
    body = json.dumps({"app": args.app, "sender": args.sender, "text": args.text}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{settings.port}/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings.access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
        return 0
    except urllib.error.URLError as exc:
        print(f"Could not reach Hushbell on port {settings.port}: {exc}. Is `hushbell run` going?")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
