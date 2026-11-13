import io
import json

from hushbell.config import Settings
from hushbell.setup_wizard import read_env_values, run_wizard, set_env_values
from hushbell.tunnel import detect_ngrok


def _opener(payload):
    def opener(url, timeout=0):
        return io.BytesIO(json.dumps(payload).encode())
    return opener


def test_ngrok_address_is_found_for_our_port():
    tunnels = {"tunnels": [
        {"public_url": "https://other.ngrok.app", "config": {"addr": "http://localhost:3000"}},
        {"public_url": "https://hush.ngrok.app", "config": {"addr": "http://localhost:8000"}},
        {"public_url": "http://hush.ngrok.app", "config": {"addr": "http://localhost:8000"}},
    ]}
    assert detect_ngrok(8000, opener=_opener(tunnels)) == "https://hush.ngrok.app"
    assert detect_ngrok(9999, opener=_opener(tunnels)) == ""            # two candidates, neither is ours
    assert detect_ngrok(8000, opener=_opener({"tunnels": []})) == ""


def test_ngrok_not_running_is_fine():
    def refuse(url, timeout=0):
        raise OSError("connection refused")
    assert detect_ngrok(8000, opener=refuse) == ""


def test_env_file_values_are_replaced_in_place(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nANTHROPIC_API_KEY=\n# HUSHBELL_PORT=8000\nHUSHBELL_CHILDREN=Maya\n", encoding="utf-8")
    set_env_values(env, {"ANTHROPIC_API_KEY": "sk-test", "HUSHBELL_PORT": "9000", "HUSHBELL_SCHOOLS": "Lincoln"})
    assert env.read_text(encoding="utf-8").splitlines() == [
        "# comment", "ANTHROPIC_API_KEY=sk-test", "HUSHBELL_PORT=9000", "HUSHBELL_CHILDREN=Maya",
        "HUSHBELL_SCHOOLS=Lincoln",
    ]
    assert read_env_values(env)["HUSHBELL_CHILDREN"] == "Maya"


def test_wizard_keeps_existing_answers_when_you_press_enter(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HUSHBELL_CHILDREN=Maya\nHUSHBELL_PARENT_NAME=\n", encoding="utf-8")
    answers = iter(["", "Ana Lopez"])
    run_wizard(env, ask=lambda q: next(answers, ""), ask_secret=lambda q: "", out=lambda *a: None)
    values = read_env_values(env)
    assert values["HUSHBELL_CHILDREN"] == "Maya" and values["HUSHBELL_PARENT_NAME"] == "Ana Lopez"


def test_wizard_saves_each_answer_right_away_and_confirms_hidden_ones(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    said = []

    def stop_after_children(question):
        if "children" in question:
            return "Anna"
        raise KeyboardInterrupt                                # the parent closes the terminal halfway

    try:
        run_wizard(env, ask=stop_after_children, ask_secret=lambda q: "sk-ant-test-123", out=lambda *a: said.append(a[0]))
    except KeyboardInterrupt:
        pass
    values = read_env_values(env)
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-test-123" and values["HUSHBELL_CHILDREN"] == "Anna"
    assert "  saved (15 characters)" in said                   # confirms the hidden key without showing it


KEY = "sk-ant-api03-" + "Ab3_x-" * 15


def test_api_key_cleanup():
    from hushbell.setup_wizard import clean_api_key, key_problem

    assert clean_api_key(KEY) == (KEY, "")
    assert clean_api_key(f'  "{KEY}"  ')[0] == KEY
    # Hidden input: the key pasted three times, then a command pasted after it.
    messy = KEY * 3 + r".\.venv\Scripts\hushbell.exe run --no-phone --dry-run --demo"
    key, note = clean_api_key(messy)
    assert key == KEY and "pasted 3 times" in note
    assert clean_api_key(KEY + " run the demo")[0] == KEY
    assert clean_api_key("GPWxFEjgfQKTJJQr")[0] == ""                  # not a Claude key at all
    assert clean_api_key(KEY + "sk-ant-api03-somethingelse")[0] == ""  # two different keys
    assert key_problem(KEY) == "" and key_problem("") == ""
    assert "extra text" in key_problem(messy)
    assert "doesn't look like" in key_problem("hello")


def test_wizard_repairs_a_key_pasted_twice(tmp_path):
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    said = []
    secrets = iter([KEY + KEY, ""])
    run_wizard(env, ask=lambda q: "", ask_secret=lambda q: next(secrets, ""), out=lambda *a: said.append(a[0]))
    assert read_env_values(env)["ANTHROPIC_API_KEY"] == KEY
    assert "  The key was pasted 2 times. Saved one copy." in said


def test_settings_read_from_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HUSHBELL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HUSHBELL_CHILDREN", "Maya, Leo")
    monkeypatch.setenv("HUSHBELL_APPS", "com.parentsquare.psapp=ParentSquare:school")
    monkeypatch.delenv("HUSHBELL_ACCESS_TOKEN", raising=False)
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "HUSHBELL_PARENT_PHONE"):
        monkeypatch.delenv(name, raising=False)
    s = Settings.from_env()
    assert s.children == ["Maya", "Leo"]
    assert s.apps["com.parentsquare.psapp"] == ("ParentSquare", "school")
    assert s.calls_enabled is False
    assert len(s.access_token) >= 16 and Settings.from_env().access_token == s.access_token   # stable across runs
