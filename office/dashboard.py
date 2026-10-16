"""
Agent Office dashboard — a read-only view of Relay and the A2A ledger.

    python -m office --relay-db relay.db --a2a-db a2a.db --out office.html

Writes one self-contained HTML page (no scripts, no network, no external
fonts) showing what is waiting on the boss, every Relay task with its latest
verification, the A2A ledger, audit-chain health, the in-tray job results
(``python -m office.jobs``) and recent activity by desk.

It can look, not touch:
- both databases are opened in SQLite read-only URI mode, so their contents
  are never changed; a missing database is reported, never created (for a
  database in WAL mode SQLite may leave its usual -wal/-shm side files);
- nothing is executed: every "do this next" item is a command for the operator
  to run;
- every value from the databases is HTML-escaped (task text, actor names and
  evidence references are untrusted input).
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from relay.state import GENESIS_HASH, event_hash

from .jobs import DEFAULT_INTRAY, JOBS

# Statuses that need the boss, with the command that resolves them.
_RELAY_WAITING = {
    "BLOCKED": ("Approve or reject a manual task",
                "relay approve-task {id} --actor <you>   |   relay reject-task {id} --actor <you> --reason ..."),
    "ACCEPTANCE_REQUIRED": ("Check criteria Relay could not verify",
                            "relay acknowledge-criteria {id} --actor <you> [--reject]"),
    "RECOVERY_REQUIRED": ("Review an interrupted run",
                          "relay accept-recovery {id} --actor <you> --workspace <base>"),
    "TIMEOUT": ("Decide whether to retry a timed-out run",
                "relay retry-task {id} --actor <you>"),
}
_A2A_WAITING = {
    "AWAITING_GATE_1": ("Gate 1: approve the spec", "a2a advance {id} --to IN_IMPLEMENTATION --actor <you> --role HUMAN"),
    "AWAITING_GATE_2": ("Gate 2: merge and ship", "a2a advance {id} --to SHIPPED --actor <you> --role HUMAN --evidence <url>"),
    "BLOCKED_BY_DISCOVERY": ("Decide on a discovery", "a2a advance {id} --to IN_IMPLEMENTATION --actor <you> --role HUMAN --reason ... --evidence <url>"),
}
_DESKS = {
    "HUMAN": "The Boss", "CHATGPT": "ChatGPT · The Architect",
    "CLAUDE": "Claude · The Builder", "relay": "Relay · Inspector & Clerk",
}


@dataclass
class Snapshot:
    relay_found: bool = False
    a2a_found: bool = False
    relay_error: str | None = None
    a2a_error: str | None = None
    relay_tasks: list[dict] = field(default_factory=list)
    a2a_tasks: list[dict] = field(default_factory=list)
    waiting: list[dict] = field(default_factory=list)
    activity: list[dict] = field(default_factory=list)
    chain: dict = field(default_factory=dict)
    ledger_problems: list[str] = field(default_factory=list)
    intray: list[dict] = field(default_factory=list)


def _open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _verify_chain(rows: list[sqlite3.Row]) -> dict:
    """Same rules as StateManager.verify_audit_chain, on a read-only connection."""
    problems, prev, expected = [], GENESIS_HASH, 1
    for r in rows:
        if r["sequence"] != expected:
            problems.append(f"sequence gap before #{r['sequence']}")
            expected = r["sequence"]
        if not r["hash"]:
            problems.append(f"#{r['sequence']} has no hash")
        else:
            if r["prev_hash"] != prev:
                problems.append(f"#{r['sequence']} link broken")
            if event_hash(r["prev_hash"] or "", r["sequence"], r["event_id"], r["event_type"],
                          r["task_id"], r["execution_id"], r["timestamp"], r["actor"],
                          r["details"]) != r["hash"]:
                problems.append(f"#{r['sequence']} edited")
        prev = r["hash"] or ""
        expected += 1
    head = rows[-1]["hash"] if rows and rows[-1]["hash"] else None
    return {"count": len(rows), "problems": problems, "head": head}


def load_relay(path: Path, snap: Snapshot) -> None:
    if not path.is_file():
        return
    snap.relay_found = True
    try:
        with closing(_open_ro(path)) as conn:
            tasks = conn.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
            events = conn.execute("SELECT * FROM events ORDER BY sequence").fetchall()
            has_hash = "hash" in {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    except sqlite3.Error as exc:
        snap.relay_error = f"could not read: {exc}"
        return
    latest_verification: dict[str, dict] = {}
    for e in events:
        if e["event_type"] == "VERIFICATION_COMPLETED":
            try:
                details = json.loads(e["details"])
            except (TypeError, ValueError):
                details = None
            latest_verification[e["task_id"]] = details if isinstance(details, dict) else {}
    def _strings(value) -> list[str]:
        return [str(x) for x in value] if isinstance(value, list) else []

    for t in tasks:
        v = latest_verification.get(t["task_id"], {})
        results = v.get("acceptance_results")
        results = results if isinstance(results, dict) else {}
        snap.relay_tasks.append({
            "id": t["task_id"], "status": t["status"], "objective": t["objective"],
            "attempts": f"{t['attempt_count']}/{t['max_execution_attempts']}",
            "updated": t["updated_at"], "passed": v.get("passed"),
            "changed": _strings(v.get("files_changed")),
            "violations": _strings(v.get("protected_violations")),
            "unverified": [str(c) for c, verdict in results.items() if verdict == "UNVERIFIED"],
        })
        if t["status"] in _RELAY_WAITING:
            what, cmd = _RELAY_WAITING[t["status"]]
            snap.waiting.append({"source": "Relay", "id": t["task_id"], "what": what,
                                 "detail": t["objective"], "command": cmd.format(id=t["task_id"])})
    snap.chain = _verify_chain(events) if has_hash else {"count": len(events), "problems":
                 ["audit log predates the hash chain (open it once with relay to upgrade)"],
                 "head": None}
    for e in events[-15:]:
        snap.activity.append({"when": e["timestamp"], "desk": _DESKS.get(e["actor"], e["actor"]),
                              "what": e["event_type"].replace("_", " ").lower(),
                              "task": e["task_id"]})


def load_a2a(path: Path, snap: Snapshot) -> None:
    if not path.is_file():
        return
    snap.a2a_found = True
    try:
        with closing(_open_ro(path)) as conn:
            tasks = conn.execute("SELECT * FROM a2a_tasks ORDER BY updated_at DESC").fetchall()
            history: dict[str, list] = {}
            if _table_exists(conn, "a2a_transitions"):
                for r in conn.execute("SELECT * FROM a2a_transitions ORDER BY seq"):
                    history.setdefault(r["task_id"], []).append(r)
            reruns = set()
            if _table_exists(conn, "a2a_human_verifications"):
                reruns = {(r["task_id"], r["gate2_entry_seq"]) for r in conn.execute(
                    "SELECT task_id, gate2_entry_seq FROM a2a_human_verifications")}
    except sqlite3.Error as exc:
        snap.a2a_error = f"could not read: {exc}"
        return
    for t in tasks:
        steps = history.get(t["task_id"], [])
        last = steps[-1] if steps else None
        if not steps:
            snap.ledger_problems.append(f"{t['task_id']}: no recorded history")
        elif last["to_state"] != t["state"]:
            snap.ledger_problems.append(f"{t['task_id']}: state differs from its last recorded step")
        snap.a2a_tasks.append({
            "id": t["task_id"], "title": t["title"], "tier": t["tier"], "state": t["state"],
            "issue": t["github_issue_url"],
            "last": (f"{last['actor']} [{last['role']}]" if last else "-"),
            "steps": len(steps),
        })
        if t["state"] in _A2A_WAITING:
            what, cmd = _A2A_WAITING[t["state"]]
            if t["state"] == "AWAITING_GATE_2" and t["tier"] == "T3":
                window = max((s["seq"] for s in steps if s["to_state"] == "AWAITING_GATE_2"), default=0)
                if (t["task_id"], window) not in reruns:
                    what = "Gate 2 (T3): re-run a check yourself first"
                    cmd = ("a2a record-human-verification {id} --actor <you> "
                           "--command \"...\" --output-file out.txt")
            snap.waiting.append({"source": "A2A", "id": t["task_id"], "what": what,
                                 "detail": f"{t['tier']} · {t['title']}",
                                 "command": cmd.format(id=t["task_id"])})
        for s in steps[-5:]:
            snap.activity.append({"when": s["timestamp"], "desk": _DESKS.get(s["role"], s["role"]),
                                  "what": f"{s['from_state'] or 'created'} → {s['to_state']}"
                                          + (f" ({s['actor']})" if s["actor"] else ""),
                                  "task": s["task_id"]})


def load_intray(folder: Path, snap: Snapshot) -> None:
    """Latest result of each in-tray job (files written by office.jobs)."""
    for name in JOBS:
        try:
            data = json.loads((Path(folder) / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        findings = data.get("findings")
        snap.intray.append({
            **{k: ("" if data.get(k) is None else str(data.get(k)))
               for k in ("job", "title", "desk", "level", "status", "summary",
                         "next_step", "generated_at")},
            "findings": [str(f) for f in findings] if isinstance(findings, list) else [],
        })


def snapshot(relay_db: Path, a2a_db: Path, intray: Path | None = None) -> Snapshot:
    snap = Snapshot()
    load_relay(relay_db, snap)
    load_a2a(a2a_db, snap)
    if intray is not None:
        load_intray(intray, snap)
    snap.activity.sort(key=lambda a: a["when"], reverse=True)
    snap.activity = snap.activity[:20]
    return snap


# ── Rendering ─────────────────────────────────────────────────────────────────

def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


_STATUS_COLOURS = {
    "COMPLETED": "#DFF3EA", "SHIPPED": "#DFF3EA", "CLOSED": "#E8ECF2",
    "FAILED": "#FFE2DA", "REJECTED": "#FFE2DA", "RECOVERY_REQUIRED": "#FFE2DA",
    "ACCEPTANCE_REQUIRED": "#FFF4DE", "BLOCKED": "#FFF4DE", "TIMEOUT": "#FFF4DE",
    "AWAITING_GATE_1": "#FFF4DE", "AWAITING_GATE_2": "#FFF4DE",
    "OK": "#DFF3EA", "READY": "#E1EEFF", "NEEDS_YOU": "#FFF4DE",
    "NEEDS_CONNECTION": "#EEF2F8", "ALERT": "#FFE2DA", "ERROR": "#FFE2DA",
}


def _pill(text) -> str:
    text = str(text)
    colour = _STATUS_COLOURS.get(text, "#EEF2F8")
    return f'<span class="pill" style="background:{colour}">{_e(text)}</span>'


def render(snap: Snapshot, relay_db: Path, a2a_db: Path) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    shipped = sum(1 for t in snap.a2a_tasks if t["state"] in ("SHIPPED", "CLOSED"))
    done = sum(1 for t in snap.relay_tasks if t["status"] == "COMPLETED")
    parts = [f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent Office HQ — dashboard</title>
<style>
:root{{--ink:#1E2A3B;--muted:#4A5872;--paper:#FFFFFF;--ground:#E3F1EC;--line:#C7D3CF;--accent:#E8573A}}
@media (prefers-color-scheme: dark){{:root{{--ink:#E8EEF6;--muted:#AFBBD0;--paper:#18212F;--ground:#0F1620;--line:#33415A;--accent:#FF7A5C}}
.pill{{color:#1E2A3B}}}}
body{{margin:0;background:var(--ground);color:var(--ink);font-family:"Segoe UI",system-ui,sans-serif}}
main{{max-width:1300px;margin:0 auto;padding:20px 16px;display:flex;flex-direction:column;gap:18px}}
header{{background:var(--ink);color:var(--ground);border-radius:16px;padding:16px 20px;display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:center}}
header h1{{margin:0;font-size:26px}} header p{{margin:2px 0 0;opacity:.85}}
.stats{{display:flex;flex-wrap:wrap;gap:8px}} .stat{{background:rgba(255,255,255,.12);border-radius:10px;padding:6px 12px;font-weight:700}}
section{{background:var(--paper);border:2px solid var(--ink);border-radius:16px;padding:16px}}
h2{{margin:0 0 10px;font-size:20px}} .muted{{color:var(--muted);font-size:14px}}
table{{width:100%;border-collapse:collapse;font-size:14px}} th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}}
.scroll{{overflow-x:auto}} .pill{{display:inline-block;padding:2px 9px;border-radius:999px;border:1.5px solid #1E2A3B;font-weight:700;font-size:12px}}
.wait{{display:flex;flex-direction:column;gap:10px}} .card{{border:2px solid var(--accent);border-radius:12px;padding:10px 12px}}
code{{font-family:Consolas,ui-monospace,monospace;font-size:13px;word-break:break-all}}
.ok{{color:#1F7A4F;font-weight:700}} .bad{{color:#B03A20;font-weight:700}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}}
</style></head><body><main>
<header><div><h1>Agent Office HQ</h1><p>Read-only snapshot · {_e(now)}</p></div>
<div class="stats"><span class="stat">Waiting on you: {len(snap.waiting)}</span>
<span class="stat">In-tray: {len(snap.intray)}</span>
<span class="stat">Relay tasks completed: {done}</span><span class="stat">Shipped: {shipped}</span></div></header>"""]

    parts.append('<section aria-labelledby="w"><h2 id="w">Waiting on you</h2>')
    if snap.waiting:
        parts.append('<div class="wait">')
        for w in snap.waiting:
            parts.append(f'<div class="card"><strong>{_e(w["what"])}</strong> · {_e(w["source"])} '
                         f'<code>{_e(w["id"])}</code><div class="muted">{_e(w["detail"])}</div>'
                         f'<div><code>{_e(w["command"])}</code></div></div>')
        parts.append("</div>")
    else:
        parts.append('<p class="muted">Nothing. The office is running itself, within the rules.</p>')
    parts.append("</section>")

    parts.append('<section aria-labelledby="t"><h2 id="t">In-tray</h2>')
    if snap.intray:
        parts.append('<div class="wait">')
        for j in snap.intray:
            findings = j.get("findings") or []
            parts.append(
                f'<div class="card" style="border-color:var(--line)">{_pill(j.get("status", "?"))} '
                f'<strong>{_e(j.get("title"))}</strong> · {_e(j.get("desk"))} · '
                f'<span class="muted">{_e(j.get("level"))} · {_e(str(j.get("generated_at", ""))[:16].replace("T", " "))}</span>'
                f'<div>{_e(j.get("summary"))}</div>'
                + ("<ul>" + "".join(f"<li>{_e(f)}</li>" for f in findings[:8]) + "</ul>" if findings else "")
                + (f'<div><code>{_e(j.get("next_step"))}</code></div>' if j.get("next_step") else "")
                + "</div>")
        parts.append("</div>")
    else:
        parts.append('<p class="muted">Empty. Run <code>python -m office.jobs list</code> to see the jobs.</p>')
    parts.append("</section>")

    parts.append('<div class="grid">')
    parts.append('<section aria-labelledby="c"><h2 id="c">Integrity</h2>')
    if snap.relay_error:
        parts.append(f'<p class="bad">Relay database {_e(snap.relay_error)}</p>')
    elif snap.relay_found:
        ch = snap.chain
        if ch.get("problems"):
            parts.append(f'<p class="bad">Audit chain: {len(ch["problems"])} problem(s) in {ch["count"]} events</p><ul>'
                         + "".join(f"<li>{_e(p)}</li>" for p in ch["problems"][:10]) + "</ul>")
        else:
            parts.append(f'<p class="ok">Audit chain intact · {ch.get("count", 0)} events</p>'
                         f'<p class="muted">Head hash <code>{_e(ch.get("head"))}</code></p>')
    else:
        parts.append(f'<p class="muted">No Relay database at <code>{_e(relay_db)}</code>.</p>')
    if snap.a2a_error:
        parts.append(f'<p class="bad">A2A database {_e(snap.a2a_error)}</p>')
    elif snap.a2a_found:
        if snap.ledger_problems:
            parts.append(f'<p class="bad">Ledger: {len(snap.ledger_problems)} problem(s)</p><ul>'
                         + "".join(f"<li>{_e(p)}</li>" for p in snap.ledger_problems[:10]) + "</ul>")
        else:
            parts.append('<p class="ok">Ledger states match their recorded history</p>')
        parts.append('<p class="muted">Run <code>a2a check</code> for the full rule-by-rule replay.</p>')
    else:
        parts.append(f'<p class="muted">No A2A database at <code>{_e(a2a_db)}</code>.</p>')
    parts.append("</section>")

    parts.append('<section aria-labelledby="a"><h2 id="a">Recent activity by desk</h2>')
    if snap.activity:
        parts.append('<div class="scroll"><table><thead><tr><th>When (UTC)</th><th>Desk</th><th>What</th><th>Task</th></tr></thead><tbody>')
        for a in snap.activity:
            parts.append(f"<tr><td>{_e(a['when'][:19].replace('T', ' '))}</td><td>{_e(a['desk'])}</td>"
                         f"<td>{_e(a['what'])}</td><td><code>{_e(a['task'])}</code></td></tr>")
        parts.append("</tbody></table></div>")
    else:
        parts.append('<p class="muted">No activity yet.</p>')
    parts.append("</section></div>")

    parts.append('<section aria-labelledby="r"><h2 id="r">Relay tasks</h2>')
    if snap.relay_tasks:
        parts.append('<div class="scroll"><table><thead><tr><th>Task</th><th>Status</th><th>Attempts</th>'
                     '<th>Objective</th><th>Last verification</th></tr></thead><tbody>')
        for t in snap.relay_tasks:
            if t["passed"] is None:
                ver = '<span class="muted">not verified</span>'
            else:
                ver = (('<span class="ok">passed</span>' if t["passed"] else '<span class="bad">failed</span>')
                       + (f"<div class='muted'>changed: {_e(', '.join(t['changed']) or '-')}</div>")
                       + ("".join(f"<div class='bad'>{_e(v)}</div>" for v in t["violations"]))
                       + (f"<div class='muted'>unverified: {_e('; '.join(t['unverified']))}</div>"
                          if t["unverified"] else ""))
            parts.append(f"<tr><td><code>{_e(t['id'])}</code></td><td>{_pill(t['status'])}</td>"
                         f"<td>{_e(t['attempts'])}</td><td>{_e(t['objective'])}</td><td>{ver}</td></tr>")
        parts.append("</tbody></table></div>")
    else:
        parts.append('<p class="muted">No Relay tasks.</p>')
    parts.append("</section>")

    parts.append('<section aria-labelledby="l"><h2 id="l">A2A ledger</h2>')
    if snap.a2a_tasks:
        parts.append('<div class="scroll"><table><thead><tr><th>Task</th><th>Tier</th><th>State</th>'
                     '<th>Title</th><th>Last step by</th><th>Steps</th></tr></thead><tbody>')
        for t in snap.a2a_tasks:
            parts.append(f"<tr><td><code>{_e(t['id'])}</code></td><td>{_e(t['tier'])}</td>"
                         f"<td>{_pill(t['state'])}</td><td>{_e(t['title'])}</td>"
                         f"<td>{_e(t['last'])}</td><td>{_e(t['steps'])}</td></tr>")
        parts.append("</tbody></table></div>")
    else:
        parts.append('<p class="muted">No ledger tasks.</p>')
    parts.append("</section>")
    parts.append('<p class="muted">This page is a snapshot generated by <code>python -m office</code>. '
                 'It changes nothing; every action above is a command for you to run.</p>')
    parts.append("</main></body></html>\n")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office",
                                     description="Read-only Agent Office dashboard (writes one HTML file)")
    parser.add_argument("--relay-db", type=Path, default=Path("relay.db"))
    parser.add_argument("--a2a-db", type=Path, default=Path("a2a.db"))
    parser.add_argument("--out", type=Path, default=Path("office.html"))
    parser.add_argument("--intray", type=Path, default=DEFAULT_INTRAY)
    args = parser.parse_args(argv)
    snap = snapshot(args.relay_db, args.a2a_db, args.intray)
    args.out.write_text(render(snap, args.relay_db, args.a2a_db), encoding="utf-8")
    print(f"Wrote {args.out} - waiting on you: {len(snap.waiting)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
