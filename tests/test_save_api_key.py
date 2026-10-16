"""
demo/save_api_key.py — the paste-your-key helper for the real run.

Fake keys and a temporary key file only; the clipboard, the prompt and the
Anthropic check are all replaced, so nothing leaves the machine.
"""
from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("save_api_key", ROOT / "demo" / "save_api_key.py")
sak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sak)

FAKE = "sk-ant-api03-" + "x" * 80 + "WXYZ"


@pytest.fixture
def env(monkeypatch, tmp_path):
    state = {"clipboard": "", "cleared": False, "typed": "", "answer": "", "checked": []}
    monkeypatch.setattr(sak, "read_clipboard", lambda: state["clipboard"])
    monkeypatch.setattr(sak, "clear_clipboard", lambda: state.update(cleared=True))
    monkeypatch.setattr(sak.getpass, "getpass", lambda prompt: state["typed"])
    monkeypatch.setattr("builtins.input", lambda prompt: state["answer"])

    def fake_check(key):
        state["checked"].append(key)
        return True, "Anthropic accepted the key."
    monkeypatch.setattr(sak, "check_with_anthropic", fake_check)
    state["file"] = tmp_path / "key.txt"
    return state


def _main(env, *extra):
    return sak.main(["--key-file", str(env["file"]), *extra])


@pytest.mark.parametrize("key,why", [
    ("", "nothing was pasted"),
    ("sk-ant-api03-short", "too short"),
    ("hello" + "x" * 50, 'does not start with "sk-ant-"'),
    (FAKE[:20] + " " + FAKE[20:], "spaces"),
    (FAKE + "é", "unusual characters"),
])
def test_bad_keys_are_explained(key, why):
    assert why in sak.problem_with(key)


def test_good_key_shape():
    assert sak.problem_with(FAKE) is None
    assert sak.masked(FAKE) == "sk-ant-...WXYZ"


def test_pasted_key_is_saved_exactly_and_never_printed(env, capsys):
    env["typed"] = "  " + FAKE + "  "
    assert _main(env) == 0
    assert env["file"].read_text(encoding="ascii") == FAKE
    out = capsys.readouterr().out
    assert FAKE not in out and "sk-ant-...WXYZ" in out and "All set" in out
    assert env["checked"] == [FAKE] and env["cleared"] is False


def test_clipboard_key_is_used_and_clipboard_cleared(env):
    env["clipboard"] = FAKE
    assert _main(env) == 0
    assert env["file"].read_text(encoding="ascii") == FAKE and env["cleared"] is True


def test_declining_the_clipboard_falls_back_to_the_prompt(env):
    env["clipboard"], env["answer"] = FAKE, "n"
    env["typed"] = FAKE.replace("WXYZ", "ABCD")
    assert _main(env) == 0
    assert env["file"].read_text(encoding="ascii").endswith("ABCD") and env["cleared"] is False


def test_bad_paste_leaves_the_existing_file_alone(env, capsys):
    env["file"].write_text("previous", encoding="ascii")
    env["typed"] = "not a key"
    assert _main(env) == 1
    assert env["file"].read_text(encoding="ascii") == "previous"
    assert "Not saved" in capsys.readouterr().out


def test_rejected_key_exits_2(env, monkeypatch, capsys):
    env["typed"] = FAKE
    monkeypatch.setattr(sak, "check_with_anthropic", lambda key: (False, "rejected (401)"))
    assert _main(env) == 2
    assert "Problem: rejected (401)" in capsys.readouterr().out


def test_no_check_skips_anthropic(env):
    env["typed"] = FAKE
    assert _main(env, "--no-check") == 0 and env["checked"] == []


@pytest.mark.parametrize("code,needle", [(401, "rejected the key"), (403, "not allowed"),
                                         (529, "HTTP 529")])
def test_check_maps_http_errors(monkeypatch, code, needle):
    def fake_urlopen(req, timeout):
        assert req.full_url == sak.MODELS_URL
        assert req.get_header("X-api-key") == FAKE
        raise urllib.error.HTTPError(req.full_url, code, "err", {}, io.BytesIO(b""))
    monkeypatch.setattr(sak.urllib.request, "urlopen", fake_urlopen)
    ok, message = sak.check_with_anthropic(FAKE)
    assert ok is False and needle in message and FAKE not in message


def test_check_accepts_a_models_list(monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(sak.urllib.request, "urlopen",
                        lambda req, timeout: Resp(b'{"data": [], "has_more": false}'))
    assert sak.check_with_anthropic(FAKE) == (True, "Anthropic accepted the key.")
