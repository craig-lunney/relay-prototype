"""
Save your Anthropic API key for Relay's real run, and check it works.

    python demo/save_api_key.py            (or double-click "Save API key.bat")

1. Copy your key (it starts with "sk-ant-") from the Anthropic Console.
2. Run this. If the key is on your clipboard it offers to use it; otherwise
   paste it at the hidden prompt and press Enter.
3. It saves the key to ~/.relay_api_key.txt (or --key-file), then asks
   Anthropic's free "list models" endpoint whether the key is accepted.
   No tokens are used and nothing is charged.

The key is never printed: only its last four characters are shown. If the key
came from the clipboard, the clipboard is cleared afterwards.
"""
from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_KEY_FILE = Path.home() / ".relay_api_key.txt"
MODELS_URL = "https://api.anthropic.com/v1/models?limit=1"


def problem_with(key: str) -> str | None:
    """Why this doesn't look like an Anthropic API key, or None if it does."""
    if not key:
        return "nothing was pasted"
    if not key.isascii() or any(ch.isspace() for ch in key):
        return "it contains spaces, line breaks or unusual characters"
    if not key.startswith("sk-ant-"):
        return 'it does not start with "sk-ant-"'
    if len(key) < 40:
        return "it is too short; check you copied all of it"
    return None


def masked(key: str) -> str:
    return f"sk-ant-...{key[-4:]}"


def read_clipboard() -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def clear_clipboard() -> None:
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $null"],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass


def check_with_anthropic(key: str) -> tuple[bool, str]:
    """Ask the free models endpoint whether the key is accepted."""
    req = urllib.request.Request(MODELS_URL, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https URL
            json.loads(resp.read())
            return True, "Anthropic accepted the key."
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, ("Anthropic rejected the key (401). It may have been mistyped, "
                           "revoked, or created in a different organisation.")
        if exc.code == 403:
            return False, "The key was recognised but is not allowed to use the API (403)."
        return False, f"Anthropic answered HTTP {exc.code}; try again in a minute."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Could not reach Anthropic to check the key ({exc}). It is saved anyway."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--key-file", type=Path, default=DEFAULT_KEY_FILE)
    parser.add_argument("--no-check", action="store_true", help="save without asking Anthropic")
    args = parser.parse_args(argv)

    print("Relay: save your Anthropic API key\n")
    key, from_clipboard = "", False
    clip = read_clipboard()
    if problem_with(clip) is None:
        answer = input(f"Found a key on your clipboard ({masked(clip)}). Use it? [Y/n] ").strip().lower()
        if answer in ("", "y", "yes"):
            key, from_clipboard = clip, True
    if not key:
        key = getpass.getpass("Paste your key and press Enter (it will not show on screen): ").strip()

    why = problem_with(key)
    if why:
        print(f"\nNot saved: {why}.")
        return 1

    args.key_file.write_text(key, encoding="ascii")
    print(f"\nSaved {masked(key)} to {args.key_file}")
    if from_clipboard:
        clear_clipboard()
        print("Clipboard cleared.")

    if args.no_check:
        return 0
    ok, message = check_with_anthropic(key)
    print(("OK: " if ok else "Problem: ") + message)
    if ok:
        print("\nAll set. Tell Claude the key is saved, or run:  python demo/real_run.py")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
