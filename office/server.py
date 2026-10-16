"""
Agent Office — the office as a small app on your own computer.

    python -m office.server            # opens http://127.0.0.1:8765/ in your browser

The dashboard, plus a button for every in-tray job. "Run" does the job's safe,
offline part; "Run live" (shown in orange) lets it read the live server or
website. Jobs only ever read and report or prepare drafts: anything that would
change a live system stays a plan in your in-tray for you to approve.

It can only be used from this computer:
- it listens on 127.0.0.1 only and refuses requests addressed to any other host
  name (so a web page cannot reach it through DNS tricks);
- every button carries a one-time code created when the app starts, so another
  website cannot press the buttons for you;
- the page may not be framed by other sites and loads nothing from outside.
One job runs at a time; the page refreshes itself while a job is running.
"""
from __future__ import annotations

import argparse
import hmac
import html
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs

from .dashboard import render, snapshot
from .jobs import DEFAULT_CONFIG, DEFAULT_INTRAY, JOBS, uk_time

ROOT = Path(__file__).resolve().parent.parent
# The buttons each job gets: (label, live). A live-only job has no offline button,
# because its offline run only says "ready" (and would hide the last live result).
BUTTONS: dict[str, list[tuple[str, bool]]] = {
    "crontab-guard": [("Run live", True)],
    "morning-briefing": [("Run", False), ("Run live", True)],
    "social-batch": [("Run", False)],
    "category-images": [("Run live", True)],
    "reviews-replies": [("Run", False)],
}
LIVE_JOBS = frozenset(job for job, buttons in BUTTONS.items() if any(live for _, live in buttons))
JOB_TIMEOUT = 900                                 # seconds
JOB_TIMEOUTS = {"category-images": 3600}          # the audit fetches ~260 pages
MAX_BODY = 4096
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


@dataclass
class OfficeState:
    """Everything the request handler needs; one per running app."""
    relay_db: Path
    a2a_db: Path
    intray: Path
    config: Path
    port: int = 8765
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    runner: Callable[[list[str]], tuple[int, str]] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    running: str | None = None
    started_at: str | None = None
    last: dict | None = None

    def job_argv(self, job: str, live: bool) -> list[str]:
        argv = [sys.executable, "-B", "-m", "office.jobs", "--intray", str(self.intray),
                "--config", str(self.config), "run", job,
                "--relay-db", str(self.relay_db), "--a2a-db", str(self.a2a_db)]
        return argv + (["--live"] if live else [])

    def start_job(self, job: str, live: bool) -> bool:
        """Start a job in the background. False if one is already running."""
        with self.lock:
            if self.running:
                return False
            self.running = f"{JOBS[job].title}" + (" (live)" if live else "")
            self.started_at = uk_time(None, "%H:%M:%S")
        argv = self.job_argv(job, live)
        timeout = JOB_TIMEOUTS.get(job, JOB_TIMEOUT)
        threading.Thread(target=self._run, args=(argv, timeout), daemon=True).start()
        return True

    def _run(self, argv: list[str], timeout: int = JOB_TIMEOUT) -> None:
        name = self.running
        try:
            code, output = (self.runner(argv) if self.runner
                            else _subprocess_runner(argv, timeout))
        except Exception as exc:  # report, never crash the app
            code, output = -1, f"Could not run the job: {exc}"
        with self.lock:
            self.last = {"job": name, "code": code, "output": output[-6000:],
                         "finished": uk_time(None, "%H:%M:%S")}
            self.running = None
            self.started_at = None


def _subprocess_runner(argv: list[str], timeout: int = JOB_TIMEOUT) -> tuple[int, str]:
    try:
        r = subprocess.run(argv, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, f"The job took longer than {timeout // 60} minutes and was stopped."
    return r.returncode, (r.stdout + ("\n" + r.stderr if r.stderr.strip() else "")).strip()


def controls_html(state: OfficeState) -> str:
    """The 'hand out a job' panel (every value escaped)."""
    busy = state.running is not None
    parts = ['<section aria-labelledby="j"><h2 id="j">Hand out a job</h2>']
    if busy:
        parts.append(f'<p class="card"><strong>Working on: {_e(state.running)}</strong> '
                     f'· started {_e(state.started_at)} · this page refreshes by itself.</p>')
    parts.append('<div class="jobs">')
    disabled = " disabled" if busy else ""
    for name, spec in JOBS.items():
        buttons = [(label, live, ' class="live"' if live else "")
                   for label, live in BUTTONS.get(name, [])]
        forms = "".join(
            f'<form method="post" action="/run">'
            f'<input type="hidden" name="token" value="{_e(state.token)}">'
            f'<input type="hidden" name="job" value="{_e(name)}">'
            + ('<input type="hidden" name="live" value="1">' if live else "")
            + f'<button type="submit"{cls}{disabled}>{_e(label)}</button></form>'
            for label, live, cls in buttons)
        parts.append(f'<div class="job"><strong>{_e(spec.title)}</strong>'
                     f'<span class="muted">{_e(spec.desk)} · {_e(spec.level)}</span>'
                     f'<span class="muted">{_e(spec.blurb)}</span>'
                     f'<div class="btns">{forms}</div></div>')
    parts.append("</div>")
    if state.last:
        last = state.last
        verdict = "finished" if last["code"] == 0 else f"finished with exit code {last['code']}"
        parts.append(f'<h3>Last job: {_e(last["job"])} · {_e(verdict)} at {_e(last["finished"])}</h3>'
                     f'<pre>{_e(last["output"])}</pre>')
    parts.append('<p class="muted">Orange buttons read the live server or website (read-only). '
                 'Nothing here changes a live system.</p></section>')
    return "".join(parts)


class OfficeHandler(BaseHTTPRequestHandler):
    server_version = "AgentOffice/1.0"
    timeout = 10  # seconds; a stalled connection is dropped instead of holding a thread
    state: OfficeState  # set on the subclass made by make_server()

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status: int, body: str, extra: dict | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _host_ok(self) -> bool:
        port = self.state.port
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _message(self, status: int, text: str) -> None:
        self._send(status, f'<!doctype html><meta charset="utf-8"><title>Agent Office</title>'
                           f'<p>{_e(text)}</p><p><a href="/">Back to the office</a></p>')

    def do_GET(self):
        if not self._host_ok():
            return self._message(403, "This office only answers on this computer.")
        if self.path != "/":
            return self._message(404, "Nothing here.")
        s = self.state
        page = render(snapshot(s.relay_db, s.a2a_db, s.intray), s.relay_db, s.a2a_db,
                      controls=controls_html(s), refresh_seconds=5 if s.running else None,
                      subtitle="Live office")
        self._send(200, page)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._host_ok():
            return self._message(403, "This office only answers on this computer.")
        if self.path != "/run":
            return self._message(404, "Nothing here.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            return self._message(400, "Bad request.")
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), max_num_fields=10)
        except ValueError:
            return self._message(400, "Bad request.")
        token = (form.get("token") or [""])[0]
        if not hmac.compare_digest(token.encode(), self.state.token.encode()):
            return self._message(403, "That button has expired: reload the office and try again.")
        job = (form.get("job") or [""])[0]
        live = (form.get("live") or [""])[0] == "1"
        if job not in JOBS:
            return self._message(400, "Unknown job.")
        if (job and (live not in {is_live for _, is_live in BUTTONS.get(job, [])})):
            return self._message(400, "That job has no " + ("live" if live else "offline") + " mode.")
        if not self.state.start_job(job, live):
            return self._message(409, "A job is already running: wait for it to finish.")
        self.send_response(303)
        self.send_header("Location", "/")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _OfficeHTTPServer(ThreadingHTTPServer):
    """Refuses to share its port: a second copy of the office fails to start."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(state: OfficeState) -> ThreadingHTTPServer:
    handler = type("BoundOfficeHandler", (OfficeHandler,), {"state": state})
    server = _OfficeHTTPServer(("127.0.0.1", state.port), handler)
    state.port = server.server_address[1]
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office.server",
                                     description="Agent Office on this computer (127.0.0.1 only)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--relay-db", type=Path, default=ROOT / "relay.db")
    parser.add_argument("--a2a-db", type=Path, default=ROOT / "a2a.db")
    parser.add_argument("--intray", type=Path, default=DEFAULT_INTRAY)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    state = OfficeState(relay_db=args.relay_db, a2a_db=args.a2a_db, intray=args.intray,
                        config=args.config, port=args.port)
    try:
        server = make_server(state)
    except OSError as exc:
        print(f"Could not start the office on port {args.port}: {exc}\n"
              f"Is it already open? Try http://127.0.0.1:{args.port}/")
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{args.port}/")
        return 1
    url = f"http://127.0.0.1:{state.port}/"
    print(f"Agent Office is open at {url}  (close this window to shut it)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
