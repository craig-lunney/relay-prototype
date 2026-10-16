"""
Agent Office — Windows alerts.

    python -m office.notify          # pops up a notification if any in-tray job needs attention
    python -m office.notify --test   # shows a test notification

Run by the morning routine after the jobs. A notification appears only when a job
(not retired) reports ALERT or ERROR, so a quiet morning stays quiet. It uses
Windows' own notifications through PowerShell; nothing leaves this computer.
"""
from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .jobs import DEFAULT_CONFIG, DEFAULT_INTRAY, JOBS, load_result, retired_jobs

PROBLEM_STATUSES = ("ALERT", "ERROR")
ON_DEMAND = frozenset({"share-preview"})  # one-off checks: never a morning alert
# Windows PowerShell's registered app id: lets a script show a toast without installing anything.
POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"


def problems(intray: Path, retired: frozenset[str] = frozenset()) -> list[dict]:
    """In-tray results that need attention, in job order."""
    found = []
    for name in JOBS:
        if name in retired or name in ON_DEMAND:
            continue
        result = load_result(intray, name)
        if result and str(result.get("status")) in PROBLEM_STATUSES:
            found.append({"title": JOBS[name].title, "status": str(result.get("status")),
                          "summary": str(result.get("summary", ""))})
    return found


def toast_script(title: str, body: str) -> str:
    """PowerShell that shows one Windows notification.

    The text is XML-escaped and the XML travels base64-encoded, so no character in it
    (quotes of any kind included) can end a PowerShell string.
    """
    def text(value: str) -> str:
        return escape(" ".join(value.split())[:240])
    xml = ('<toast><visual><binding template="ToastGeneric">'
           f'<text>{text(title)}</text><text>{text(body)}</text>'
           '</binding></visual></toast>')
    encoded = base64.b64encode(xml.encode("utf-8")).decode("ascii")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime] > $null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        f"$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}')))\n"
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{POWERSHELL_APP_ID}')"
        ".Show($toast)\n")


def show_toast(title: str, body: str, run=subprocess.run) -> bool:
    """Show a Windows notification. False (never an exception) if it could not be shown."""
    if sys.platform != "win32" and run is subprocess.run:
        return False
    powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / \
        "WindowsPowerShell" / "v1.0" / "powershell.exe"
    encoded = base64.b64encode(toast_script(title, body).encode("utf-16-le")).decode("ascii")
    try:
        r = run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def main(argv: list[str] | None = None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(prog="office.notify", description=__doc__.split("\n\n")[0])
    parser.add_argument("--intray", type=Path, default=DEFAULT_INTRAY)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--test", action="store_true", help="show a test notification")
    args = parser.parse_args(argv)
    if args.test:
        ok = show_toast("Agent Office", "Test notification: alerts will appear like this.", run)
        print("Test notification shown." if ok else "Could not show a notification.")
        return 0 if ok else 1
    found = problems(args.intray, retired_jobs(args.config))
    if not found:
        print("No alerts.")
        return 0
    title = f"Agent Office: {len(found)} job(s) need a look"
    body = "; ".join(f"{p['title']}: {p['status']}" for p in found)
    shown = show_toast(title, body, run)
    print(f"{title} - {body}" + ("" if shown else " (notification could not be shown)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
