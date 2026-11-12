"""Settings, read from environment variables (and a .env file if present)."""

from __future__ import annotations

import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Android package name -> (label shown to the parent, kind).
# "school": every notification from the app is a school message.
# "mixed": the app also carries personal messages, so only notifications that
#          mention one of HUSHBELL_KEYWORDS are read (privacy first).
DEFAULT_APPS: dict[str, tuple[str, str]] = {
    "com.remind101": ("Remind", "school"),
    "com.classdojo.android": ("ClassDojo", "school"),
    "com.whatsapp": ("WhatsApp", "mixed"),
    "com.google.android.gm": ("Gmail", "mixed"),
}


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _apps(value: str | None) -> dict[str, tuple[str, str]]:
    """Parse HUSHBELL_APPS: "com.pkg=Label:school,com.other=Other:mixed"."""
    apps = dict(DEFAULT_APPS)
    for entry in _list(value):
        if "=" not in entry:
            continue
        pkg, rest = entry.split("=", 1)
        label, _, kind = rest.partition(":")
        apps[pkg.strip()] = (label.strip() or pkg.strip(), (kind.strip() or "school").lower())
    return apps


@dataclass
class Settings:
    data_dir: Path
    access_token: str

    # Claude
    model: str = "claude-opus-5-5"
    effort: str = "low"

    # Family context (helps sorting and form filling)
    children: list[str] = field(default_factory=list)
    parent_name: str = ""
    schools: list[str] = field(default_factory=list)
    school_address: str = ""      # for "open directions to school"
    travel_minutes: int = 30      # how long the drive to school takes, for the "time to leave" alarm
    partner_name: str = ""        # a co-parent Hushbell can text, with your OK
    partner_phone: str = ""

    # Phone (reading)
    adb_path: str = "adb"
    adb_serial: str | None = None
    poll_seconds: float = 3.0
    apps: dict[str, tuple[str, str]] = field(default_factory=lambda: dict(DEFAULT_APPS))
    keywords: list[str] = field(default_factory=list)
    read_phone: bool = True
    backfill: bool = False

    # Phone (acting)
    phone_backend: str = "mobilerun"  # "mobilerun" or "dry-run"
    mobilerun_cmd: str = "mobilerun"
    mobilerun_steps: int = 20
    mobilerun_reasoning: bool = False
    mobilerun_timeout: int = 240
    mobilerun_provider: str = ""
    mobilerun_model: str = ""
    macros: dict[str, str] = field(default_factory=dict)

    # Safety rules
    extra_urgent_words: list[str] = field(default_factory=list)

    # Calls (Twilio)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""
    parent_phone: str = ""
    public_base_url: str = ""
    twilio_validate: bool = True
    redial_seconds: int = 60

    # Web page
    host: str = "0.0.0.0"
    port: int = 8000
    demo_mode: bool = False

    # Digest
    digest_time: str = "18:00"

    @property
    def calls_enabled(self) -> bool:
        return all(
            [self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_number,
             self.parent_phone, self.public_base_url]
        )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hushbell.db"

    @property
    def graph_db_path(self) -> Path:
        return self.data_dir / "graph.db"

    @classmethod
    def from_env(cls, env_file: str | os.PathLike | None = None) -> "Settings":
        load_dotenv(env_file or ".env", override=False)
        env = os.environ.get

        data_dir = Path(env("HUSHBELL_DATA_DIR") or Path.home() / ".hushbell").expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        # A .env in the data folder also works, so `hushbell` can be started from anywhere.
        load_dotenv(data_dir / ".env", override=False)

        token = env("HUSHBELL_ACCESS_TOKEN") or _load_or_create_token(data_dir)

        macros: dict[str, str] = {}
        if env("HUSHBELL_MACROS"):
            try:
                macros = {str(k): str(v) for k, v in json.loads(env("HUSHBELL_MACROS", "{}")).items()}
            except json.JSONDecodeError:
                macros = {}

        return cls(
            data_dir=data_dir,
            access_token=token,
            model=env("HUSHBELL_MODEL") or "claude-opus-5-5",
            effort=env("HUSHBELL_EFFORT") or "low",
            children=_list(env("HUSHBELL_CHILDREN")),
            parent_name=env("HUSHBELL_PARENT_NAME") or "",
            schools=_list(env("HUSHBELL_SCHOOLS")),
            school_address=env("HUSHBELL_SCHOOL_ADDRESS") or "",
            travel_minutes=int(env("HUSHBELL_TRAVEL_MINUTES") or 30),
            partner_name=env("HUSHBELL_PARTNER_NAME") or "",
            partner_phone=env("HUSHBELL_PARTNER_PHONE") or "",
            adb_path=env("HUSHBELL_ADB") or find_adb(),
            adb_serial=env("HUSHBELL_ADB_SERIAL") or None,
            poll_seconds=float(env("HUSHBELL_POLL_SECONDS") or 3.0),
            apps=_apps(env("HUSHBELL_APPS")),
            keywords=_list(env("HUSHBELL_KEYWORDS")),
            read_phone=_bool(env("HUSHBELL_READ_PHONE"), True),
            backfill=_bool(env("HUSHBELL_BACKFILL"), False),
            phone_backend=(env("HUSHBELL_PHONE_BACKEND") or "mobilerun").lower(),
            mobilerun_cmd=env("HUSHBELL_MOBILERUN_CMD") or find_mobilerun(data_dir),
            mobilerun_steps=int(env("HUSHBELL_MOBILERUN_STEPS") or 20),
            mobilerun_reasoning=_bool(env("HUSHBELL_MOBILERUN_REASONING"), False),
            mobilerun_timeout=int(env("HUSHBELL_MOBILERUN_TIMEOUT") or 240),
            mobilerun_provider=env("HUSHBELL_MOBILERUN_PROVIDER") or "",
            mobilerun_model=env("HUSHBELL_MOBILERUN_MODEL") or "",
            macros=macros,
            extra_urgent_words=_list(env("HUSHBELL_EXTRA_URGENT_WORDS")),
            twilio_account_sid=env("TWILIO_ACCOUNT_SID") or "",
            twilio_auth_token=env("TWILIO_AUTH_TOKEN") or "",
            twilio_from_number=env("TWILIO_FROM_NUMBER") or "",
            parent_phone=env("HUSHBELL_PARENT_PHONE") or "",
            public_base_url=(env("HUSHBELL_PUBLIC_URL") or "").rstrip("/"),
            twilio_validate=_bool(env("HUSHBELL_TWILIO_VALIDATE"), True),
            redial_seconds=int(env("HUSHBELL_REDIAL_SECONDS") or 60),
            host=env("HUSHBELL_HOST") or "0.0.0.0",
            port=int(env("HUSHBELL_PORT") or 8000),
            demo_mode=_bool(env("HUSHBELL_DEMO"), False),
            digest_time=env("HUSHBELL_DIGEST_TIME") or "18:00",
        )


def find_adb() -> str:
    """`adb` if it is on PATH, else the usual install locations, else plain "adb"."""
    if shutil.which("adb"):
        return "adb"
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    candidates = [
        local / "Microsoft" / "WinGet" / "Links" / "adb.exe",
        local / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        home / "Library" / "Android" / "sdk" / "platform-tools" / "adb",
        home / "Android" / "Sdk" / "platform-tools" / "adb",
    ]
    packages = local / "Microsoft" / "WinGet" / "Packages"
    if packages.exists():  # `winget install Google.PlatformTools` puts it here
        candidates += sorted(packages.glob("Google.PlatformTools*/platform-tools/adb.exe"))
    for path in candidates:
        if path.exists():
            return str(path)
    return "adb"


def find_mobilerun(data_dir: Path) -> str:
    """`mobilerun` if it is on PATH, else a copy installed in the data folder, else plain "mobilerun"."""
    if shutil.which("mobilerun"):
        return "mobilerun"
    scripts = "Scripts" if os.name == "nt" else "bin"
    name = "mobilerun.exe" if os.name == "nt" else "mobilerun"
    managed = data_dir / "mobilerun-venv" / scripts / name
    return str(managed) if managed.exists() else "mobilerun"


def _load_or_create_token(data_dir: Path) -> str:
    """Create a random access token on first run and keep it in the data folder."""
    path = data_dir / "access_token"
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(18)
    path.write_text(token, encoding="utf-8")
    return token
