"""No garbled text: a file saved in the wrong encoding turns a middle dot into "A-hat + dot" on the big screen."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Built from code points so this file doesn't contain what it looks for.
GARBLED = (chr(0xC2) + chr(0xB7), chr(0xC2) + chr(0xB0), chr(0xE2) + chr(0x20AC), chr(0xC3) + chr(0xA9), chr(0xFEFF))


def test_source_files_have_no_garbled_characters():
    bad = []
    for path in ROOT.rglob("*"):
        if any(part in {".venv", ".git", "node_modules"} for part in path.parts) or not path.is_file():
            continue
        if path.suffix not in {".py", ".html", ".js", ".md", ".toml", ".webmanifest"}:
            continue
        text = path.read_text(encoding="utf-8")
        bad += [f"{path.relative_to(ROOT)}: {mark!r}" for mark in GARBLED if mark in text]
    assert bad == []
