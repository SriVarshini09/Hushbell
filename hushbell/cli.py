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
    run.add_argument("--simulate-calls", action="store_true",
                     help="rehearsal: with --demo and no Twilio, show the call on screen (no phone rings)")
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
    sub.add_parser("reset", help="clear all messages and the activity log, to start a rehearsal over")

    args = parser.parse_args(argv)
    # School messages can contain emoji that some Windows terminals can't print. Never crash on them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if args.command == "init":
        from pathlib import Path

        from hushbell.setup_wizard import run_wizard
        try:
            if sys.stdin.isatty():
                return run_wizard(Path(args.file))
            # Answers piped in from a script: hidden typing needs a real keyboard, so read plain lines.
            return run_wizard(Path(args.file), ask_secret=input)
        except (EOFError, KeyboardInterrupt):     # Ctrl+C, or no keyboard to ask (a script, a closed terminal)
            print(f"\nStopped. Every answer so far is saved in {args.file}. Run `hushbell init` again to go on.")
            return 1
    settings = Settings.from_env(args.env)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):     # one line per Claude call would bury the useful ones
        logging.getLogger(noisy).setLevel(logging.WARNING)

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
    if args.command == "reset":
        from hushbell.store import Store

        store = Store(settings.db_path)
        store.clear_history()
        store.add_event("start over", "All messages cleared. Ready for a fresh run")
        store.close()
        print("Cleared all messages and the activity log. Your settings are unchanged.")
        return 0
    return 1


def cmd_run(settings: Settings, args) -> int:
    import uvicorn

    from hushbell.runtime import DigestClock, PhoneReader, build_engine, make_reader_source
    from hushbell.web.app import create_app

    cloud = settings.cloud_phone      # decided before --dry-run swaps the actions out
    if args.no_phone:
        settings.read_phone = False
    if args.dry_run:
        settings.phone_backend = "dry-run"
    if args.demo:
        settings.demo_mode = True
    if args.simulate_calls:
        settings.simulate_calls = True
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port

    _use_ngrok_if_running(settings)
    inbound_note = ""
    if settings.calls_enabled and settings.twilio_autoconfig:
        from hushbell.voice import configure_inbound

        inbound_note = configure_inbound(settings)
    engine = build_engine(settings)
    app = create_app(engine)
    recovered = engine.recover()
    if recovered:
        print(f"  Picked up {recovered} message(s) that were in progress when Hushbell last stopped.")

    reader = None
    if settings.read_phone:
        source = make_reader_source(engine, cloud)
        if source.available():
            reader = PhoneReader(engine, source, settings.phoneuse_poll_seconds if cloud else None)
            reader.start()
        elif cloud:
            print("[!!] PHONEUSE_API_KEY is not set. Not reading the cloud phone. Run `hushbell doctor`.")
        else:
            print(f"[!!] adb not found ('{settings.adb_path}'). Not reading the phone. Run `hushbell doctor`.")
    clock = DigestClock(engine)
    clock.start()

    print()
    lan = _lan_ip()
    on_phone = settings.public_base_url or (f"http://{lan}:{settings.port}" if lan else
                                            f"http://<this laptop's IP>:{settings.port}")
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    from hushbell.tracing import enabled as tracing_on

    print("  HUSHBELL is running.")
    print(f"  Page:   http://localhost:{settings.port}   (on your phone: {on_phone}, or press 'Open on your phone')")
    print(f"  Token:  {settings.access_token}")
    print(f"  Sort:   {'Claude (' + settings.model + ')' if has_key else 'safety rules only: no Claude key yet (run `hushbell init`)'}")
    calls = ("on" if settings.calls_enabled
             else "SIMULATED for rehearsal: no phone will ring (keys C and P on the big screen)"
             if engine.simulated_calls else "off (set TWILIO_* and HUSHBELL_PARENT_PHONE)")
    if settings.simulate_calls and not engine.simulated_calls:
        print("  --simulate-calls is ignored: it needs --demo, and Twilio not set up (real calls win).")
    print(f"  Calls:  {calls}"
          f"{' | voice: ElevenLabs' if settings.elevenlabs_api_key and settings.calls_enabled else ''}")
    if inbound_note:
        print(f"          {inbound_note}")
    where = "PhoneUse cloud phone" if cloud else "USB Android"
    print(f"  Phone:  {where} | reading {'on' if reader else 'off'} | actions: {engine.phone.name}")
    print(f"  Ask:    Claude{' + Tavily web search' if settings.tavily_api_key else ''}"
          f"   Traces: {'LangSmith, project ' + os.environ.get('LANGSMITH_PROJECT', 'hushbell') if tracing_on() else 'off'}")
    print(f"  Big screen for a demo: http://localhost:{settings.port}/present")
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


def _lan_ip() -> str:
    """This computer's address on the local network, for opening the page from a phone."""
    from hushbell.tunnel import lan_ip

    return lan_ip()


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
    from hushbell.setup_wizard import key_problem

    bad_key = key_problem(os.environ.get("ANTHROPIC_API_KEY", ""))
    if bad_key:
        problems += 1
        _warn(f"ANTHROPIC_API_KEY is set, but {bad_key}", "run `hushbell init` and paste the key once")
    elif os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
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
    if settings.cloud_phone:
        problems += _check_phoneuse(settings, live)
    elif not shutil.which(settings.adb_path):
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
    elif settings.cloud_phone:
        _ok("replies and forms run as tasks on the PhoneUse cloud phone")
        if not settings.calls_enabled:
            _warn("texts to your co-parent and 'time to leave' calls need Twilio in cloud-phone mode")
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

    print("\nExtras (each is optional)")
    problems += _check_extras(settings, live)

    print(f"\n{'All set.' if problems == 0 else f'{problems} thing(s) to fix.'}\n")
    return 0 if problems == 0 else 1


def _check_phoneuse(settings: Settings, live: bool) -> int:
    if not settings.phoneuse_api_key:
        _warn("cloud-phone mode is on, but PHONEUSE_API_KEY is not set",
              "make a key in the PhoneUse console (console.phoneuse.com) and run `hushbell init`")
        return 1
    _ok("PhoneUse key is set; the school apps live on a cloud phone"
        + (f" ({settings.phoneuse_device})" if settings.phoneuse_device else ""))
    if not live:
        print("       run `hushbell doctor --live` to check the cloud phone is online")
        return 0
    from hushbell.phoneuse import PhoneUseClient, PhoneUseError, pick_device

    try:
        client = PhoneUseClient(settings.phoneuse_api_key, settings.phoneuse_url)
        device = pick_device(client, settings.phoneuse_device)
        image, _ = client.screenshot(device)
        _ok(f"cloud phone {device} is online (screenshot {len(image) // 1024} KB)")
        return 0
    except (PhoneUseError, Exception) as exc:  # noqa: BLE001
        _warn(f"PhoneUse check failed: {exc}")
        return 1


def _check_extras(settings: Settings, live: bool) -> int:
    from hushbell.tracing import enabled as tracing_on
    from hushbell.tunnel import detect_replit

    problems = 0
    if settings.elevenlabs_api_key:
        _ok(f"ElevenLabs voice for calls (voice {settings.elevenlabs_voice}, {settings.elevenlabs_model})")
        if live:
            from hushbell.narrator import Narrator

            if Narrator(settings).clip("This is Hushbell's voice check."):
                _ok("ElevenLabs made a test clip")
            else:
                problems += 1
                _warn("ElevenLabs didn't answer; calls will use Twilio's voice", "check ELEVENLABS_API_KEY")
    else:
        print("  [--] ElevenLabs voice: off (calls use Twilio's voice). Set ELEVENLABS_API_KEY to turn it on.")
    if settings.tavily_api_key:
        _ok("Tavily web search for 'Ask Hushbell'")
        if live:
            from hushbell.assistant import web_search

            try:
                web_search("San Francisco Unified School District calendar", settings.tavily_api_key)
                _ok("Tavily answered a test search")
            except Exception as exc:  # noqa: BLE001
                problems += 1
                _warn(f"Tavily test search failed: {exc}")
    else:
        print("  [--] Tavily web search: off. Set TAVILY_API_KEY so 'Ask Hushbell' can look things up.")
    if tracing_on():
        _ok(f"LangSmith traces, project '{os.environ.get('LANGSMITH_PROJECT', 'hushbell')}'")
    else:
        print("  [--] LangSmith traces: off. Set LANGSMITH_TRACING=true and LANGSMITH_API_KEY to see each step.")
    if detect_replit():
        _ok(f"running on Replit: {detect_replit()}")
    return problems


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
    if settings.cloud_phone:
        print("`hushbell notifications` reads an Android plugged into this laptop. In cloud-phone mode, "
              "run `hushbell doctor --live` to check the cloud phone instead.")
        return 1
    if not shutil.which(settings.adb_path):
        print(f"adb not found ('{settings.adb_path}'). Install Android platform-tools and add adb to PATH, "
              "or set HUSHBELL_ADB to its full path. `hushbell doctor` checks the rest.")
        return 1
    try:
        output = AdbNotificationSource(settings).read_raw()
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read the phone: {exc}. Is it plugged in, with USB debugging on? Run `hushbell doctor`.")
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
            answer = json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:     # Hushbell answered, so "is it running?" would be the wrong hint
        if exc.code == 401:
            print(f"Hushbell on port {settings.port} didn't accept the access token. Run `hushbell send` from the "
                  "same folder (and with the same HUSHBELL_DATA_DIR) as `hushbell run`.")
        else:
            print(f"Hushbell on port {settings.port} refused the message (HTTP {exc.code}). "
                  "The text can be up to 4000 characters, and the app and sender can't be empty.")
        return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"Could not reach Hushbell on port {settings.port}: {getattr(exc, 'reason', exc)}. "
              "Is `hushbell run` going?")
        return 1
    if answer.get("new"):
        print(f"Sent. Hushbell is sorting it now (message {answer.get('item_id')}).")
    else:
        print("Hushbell saw the same words in the last 12 hours, so it joined them to the earlier message "
              f"({answer.get('item_id')}). `hushbell reset` starts over.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
