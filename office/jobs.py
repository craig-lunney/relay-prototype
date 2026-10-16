"""
Agent Office — the morning in-tray jobs (Batch 7).

    python -m office.jobs list
    python -m office.jobs run social-batch
    python -m office.jobs run crontab-guard --live      # reads the live server
    python -m office.jobs run morning-briefing [--live]
    python -m office.jobs run reviews-replies --input reviews.json
    python -m office.jobs run category-images

Every job writes its result to the in-tray folder (default
``~/.agent-office/intray``) as ``<job>.json`` plus, where useful, a Markdown
note.  ``python -m office`` shows the in-tray with everything else waiting on
you.

Safety levels (shown on every job):
  REPORT  reads and tells you; changes nothing.
  DRAFT   prepares something for your approval; nothing goes live by itself.
  CHANGE  would alter a live system; the job only prepares the plan — the
          change itself is a separate, approved, verified step.

Jobs that read the live server or website refuse to do so without ``--live``,
so nothing touches production unless you ask for it.  No job ever writes to
the server, the website, Buffer, Reviews.io or any other external system.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from urllib.parse import urlsplit
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:  # no time-zone data: fall back to this computer's local time
    UK_TZ = None


def uk_time(value=None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A time for display in UK time (GMT or BST). Records stay in UTC.

    Accepts a datetime, an ISO-8601 string (naive means UTC) or None for now.
    An empty value gives ""; anything unparseable is returned unchanged.
    """
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(UK_TZ).strftime(fmt) if UK_TZ else dt.astimezone().strftime(fmt)


DEFAULT_INTRAY = Path.home() / ".agent-office" / "intray"
# Server details live in this local file, never in the repository:
#   {"crontab_guard": {"ssh_target": "user@host", "ssh_port": 22,
#                      "ssh_options": ["MACs=..."], "baseline_lines": 25,
#                      "required_entries": {"Magento cron": "bin/magento cron:run"}}}
DEFAULT_CONFIG = Path.home() / ".agent-office" / "config.json"
DEFAULT_SOCIAL_REPO = Path.home() / "lunneys-social"

# Used when the config names no required entries.
DEFAULT_REQUIRED_CRON_ENTRIES = {"Magento cron": "bin/magento cron:run"}
# Only these `ssh -o` options may come from the config (no ProxyCommand and the like).
_SAFE_SSH_OPTIONS = ("MACs", "Ciphers", "KexAlgorithms", "HostKeyAlgorithms", "IdentityFile",
                     "UserKnownHostsFile", "StrictHostKeyChecking", "PubkeyAcceptedAlgorithms")
SITE = "https://loshop.co.uk"


@dataclass
class JobResult:
    job: str
    title: str
    desk: str
    level: str
    status: str                 # OK | ALERT | READY | NEEDS_YOU | NEEDS_CONNECTION | ERROR
    summary: str
    findings: list[str] = field(default_factory=list)
    next_step: str = ""
    live: bool = False
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    preview: dict | None = None  # share-preview only: what a shared link shows


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    desk: str
    level: str
    live_needed: bool
    blurb: str


JOBS: dict[str, JobSpec] = {
    "backup-check": JobSpec(
        "backup-check", "Backup check", "The Inspector", "REPORT", True,
        "Checks how old the newest full database copy on the server is, and that it is not "
        "unexpectedly small."),
    "product-copy": JobSpec(
        "product-copy", "Product copy check", "The Architect", "REPORT", True,
        "Reads every enabled product (read-only) and flags names with repeated words, meta "
        "descriptions that contradict the product (upright vs cylinder, cordless vs corded...), "
        "promises to double-check (free delivery, next day...) and missing or long descriptions."),
    "share-preview": JobSpec(
        "share-preview", "Share preview", "The Designer", "REPORT", True,
        "Shows how a loshop.co.uk link will look when shared on Facebook or WhatsApp, and "
        "flags a missing, broken or badly sized share image."),
    "crontab-guard": JobSpec(
        "crontab-guard", "Crontab guard", "The Inspector", "REPORT", True,
        "Checks the live crontab has not shrunk below its baseline and that the key entries "
        "named in ~/.agent-office/config.json are live (the 3 Aug incident wiped 19 entries)."),
    "morning-briefing": JobSpec(
        "morning-briefing", "Morning briefing", "The Clerk", "REPORT", False,
        "One page: what's waiting on you, every in-tray result, and (with --live) the "
        "robots.txt and sitemap guard."),
    "social-batch": JobSpec(
        "social-batch", "This week's social batch", "ChatGPT · Claude · Gemini", "DRAFT", False,
        "Picks approved knowledge items not yet posted, spread across categories, and gives "
        "you the commands to run them through the two-gate pipeline."),
    "category-images": JobSpec(
        "category-images", "Category images backfill (CCP 3)", "Architect · Designer", "CHANGE", False,
        "CCP 3. With --live, audits every category page on the public site for a logo "
        "fallback og:image (Phase 1); then Gemini/Canva make 1200x630 images (Phase 2)."),
    "reviews-replies": JobSpec(
        "reviews-replies", "Reviews.io reply pack", "The Architect", "DRAFT", False,
        "Turns exported reviews into a reply brief; anything 3 stars or below goes to you "
        "first. Nothing is posted."),
}


# ── crontab guard ─────────────────────────────────────────────────────────────

class ConfigError(ValueError):
    """The crontab-guard section of the local config is missing or invalid."""


def retired_jobs(path: Path) -> frozenset[str]:
    """Jobs the boss has retired (config "retired_jobs": [...]); unknown names are ignored.

    Retired jobs are hidden from the office buttons, the in-tray and the morning
    briefing. They can still be run by name from the command line.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return frozenset()
    names = data.get("retired_jobs") if isinstance(data, dict) else None
    if not isinstance(names, list):
        return frozenset()
    return frozenset(n for n in names if isinstance(n, str) and n in JOBS)


def load_guard_config(path: Path) -> dict:
    """Read and validate the crontab_guard section of the local config file."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"no config file at {path}") from None
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
    cfg = data.get("crontab_guard") if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        raise ConfigError(f'{path} has no "crontab_guard" section')
    target = cfg.get("ssh_target")
    if (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+", target)
            or target.startswith("-")):
        raise ConfigError('"ssh_target" must look like user@host')
    port = cfg.get("ssh_port", 22)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError('"ssh_port" must be a number from 1 to 65535')
    options = cfg.get("ssh_options", [])
    if not isinstance(options, list) or not all(
            isinstance(o, str) and "=" in o and o.split("=", 1)[0] in _SAFE_SSH_OPTIONS
            and not any(ch.isspace() for ch in o) for o in options):
        raise ConfigError(f'"ssh_options" may only set: {", ".join(_SAFE_SSH_OPTIONS)}')
    baseline = cfg.get("baseline_lines")
    if baseline is not None and (not isinstance(baseline, int) or isinstance(baseline, bool)
                                 or baseline < 1):
        raise ConfigError('"baseline_lines" must be a positive number')
    required = cfg.get("required_entries", DEFAULT_REQUIRED_CRON_ENTRIES)
    if not isinstance(required, dict) or not required or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in required.items()):
        raise ConfigError('"required_entries" must map labels to text found in the cron line')
    return {"ssh_target": target, "ssh_port": port, "ssh_options": options,
            "baseline_lines": baseline, "required_entries": required}


def ssh_command(cfg: dict, remote: str = "crontab -l") -> list[str]:
    """A read-only command over key-based SSH (never prompts). Default: `crontab -l`."""
    cmd = ["ssh", "-p", str(cfg["ssh_port"])]
    for option in cfg["ssh_options"]:
        cmd += ["-o", option]
    return cmd + ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", cfg["ssh_target"], remote]


# ── backup check ──────────────────────────────────────────────────────────────

_SAFE_GLOB = re.compile(r"/[A-Za-z0-9_./*-]+")


def load_backup_config(path: Path) -> dict:
    """The backup_check section (plus SSH details from crontab_guard) of the local config."""
    guard = load_guard_config(path)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
    cfg = data.get("backup_check") if isinstance(data, dict) else None
    if not isinstance(cfg, dict):
        raise ConfigError(f'{path} has no "backup_check" section')
    glob = cfg.get("glob")
    if not isinstance(glob, str) or not _SAFE_GLOB.fullmatch(glob) or ".." in glob:
        raise ConfigError('"glob" must be an absolute path pattern like /home/user/backups/db_*.sql')
    days, min_mb = cfg.get("max_age_days", 7), cfg.get("min_mb", 20)
    for name, value, top in (("max_age_days", days, 60), ("min_mb", min_mb, 100_000)):
        if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= top:
            raise ConfigError(f'"{name}" must be a number from 1 to {top}')
    return {**guard, "glob": glob, "max_age_days": days, "min_mb": min_mb}


def check_backups(listing: str, now: float, max_age_days: int, min_mb: int) -> tuple[str, list[str]]:
    """(status, findings) from `stat -c '%Y %s %n'` lines, oldest first."""
    files = []
    for line in listing.splitlines():
        m = re.fullmatch(r"([0-9]{1,12}) ([0-9]{1,15}) (\S.*)", line.strip())
        if m:
            files.append((int(m.group(1)), int(m.group(2)), m.group(3)))
    if not files:
        return "ALERT", ["No full database copies found on the server."]
    files.sort()
    when, size, name = files[-1]
    age_days = (now - when) / 86400
    try:
        stamp = uk_time(datetime.fromtimestamp(when, timezone.utc))
    except (OverflowError, OSError, ValueError):
        stamp = "unknown date"
    findings = [f"Newest: {name.rsplit('/', 1)[-1]}, {stamp}, "
                f"{size / 1_048_576:.0f} MB ({age_days:.1f} days old)"]
    status = "OK"
    if age_days < -1:
        status = "ALERT"
        findings.append("The newest copy is dated in the future: the server clock or the file is wrong.")
    if len(files) > 1:
        p_when, p_size, p_name = files[-2]
        findings.append(f"Previous: {p_name.rsplit('/', 1)[-1]}, {p_size / 1_048_576:.0f} MB")
        if p_size and size < 0.8 * p_size:
            status = "ALERT"
            findings.append("The newest copy is much smaller than the previous one: check it is complete.")
    if size < min_mb * 1_048_576:
        status = "ALERT"
        findings.append(f"The newest copy is under {min_mb} MB: it may be incomplete.")
    if age_days > max_age_days:
        status = "ALERT"
        findings.append(f"The newest copy is older than {max_age_days} days. Copies are only made by "
                        "hand before site changes; Krystal's own daily backups (cPanel, JetBackup) "
                        "are the safety net in between.")
    return status, findings


def job_backup_check(args) -> JobResult:
    spec = JOBS["backup-check"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    try:
        cfg = load_backup_config(Path(args.config))
    except ConfigError as exc:
        return JobResult(**base, status="NEEDS_YOU", summary=f"Not configured: {exc}.",
                         next_step=("Add a backup_check section to ~/.agent-office/config.json "
                                    "(see docs/operator-guide.md section 9)."))
    if not args.live:
        return JobResult(**base, status="NEEDS_YOU", summary="Ready: run it live (read-only SSH).",
                         next_step="python -m office.jobs run backup-check --live")
    remote = f"stat -c '%Y %s %n' {cfg['glob']} 2>/dev/null | sort -n | tail -2"
    try:
        r = subprocess.run(ssh_command(cfg, remote), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return JobResult(**base, status="ERROR", summary=f"Could not reach the server: {exc}", live=True)
    if r.returncode != 0 and not r.stdout.strip():
        return JobResult(**base, status="ERROR", live=True,
                         summary=f"ssh exited {r.returncode}: {r.stderr.strip()[:200]}")
    status, findings = check_backups(r.stdout, time.time(), cfg["max_age_days"], cfg["min_mb"])
    return JobResult(**base, status=status, live=True, findings=findings,
                     summary=("Latest full database copy looks fine." if status == "OK"
                              else "The latest full database copy needs a look."),
                     next_step="" if status == "OK" else
                     "Before the next site change, take a fresh copy (the deploy preamble does this).")


def check_crontab(text: str, baseline_lines: int | None = None,
                  required_entries: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Return (status, findings) for a `crontab -l` listing.

    ALERT when the crontab has fewer lines than the baseline (entries lost) or a
    required entry is missing or commented out. More lines than the baseline is
    reported but is not an alert: new jobs are added on purpose.
    """
    required_entries = required_entries or DEFAULT_REQUIRED_CRON_ENTRIES
    lines = text.splitlines()
    live = [line for line in lines if not line.lstrip().startswith("#")]
    expected_lines = baseline_lines
    findings = [f"{len(lines)} lines" + (f" (baseline {expected_lines})" if expected_lines
                                         else " (no baseline set in the config)")]
    status = "OK"
    if expected_lines is None:
        pass
    elif len(lines) < expected_lines:
        status = "ALERT"
        findings.append(f"{expected_lines - len(lines)} line(s) fewer than the baseline: entries may "
                        "have been lost. Compare with the last known crontab before anything else.")
    elif len(lines) > expected_lines:
        findings.append(f"{len(lines) - expected_lines} line(s) more than the baseline. If they were "
                        "added on purpose, raise baseline_lines in ~/.agent-office/config.json.")
    for label, needle in required_entries.items():
        if not any(needle in line for line in live):
            status = "ALERT"
            findings.append(f"{label} entry (`{needle}`) is missing or commented out.")
    disabled = [line for line in lines if line.lstrip().startswith("#") and "*" in line]
    if disabled:
        findings.append(f"{len(disabled)} commented-out schedule line(s) present.")
    return status, findings


def job_crontab_guard(args) -> JobResult:
    spec = JOBS["crontab-guard"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    try:
        cfg = load_guard_config(Path(args.config))
    except ConfigError as exc:
        return JobResult(**base, status="NEEDS_YOU",
                         summary=f"Not configured: {exc}.",
                         next_step=("Create ~/.agent-office/config.json with a crontab_guard section "
                                    "(see docs/operator-guide.md section 9)."))
    if not args.live:
        return JobResult(**base, status="NEEDS_YOU",
                         summary="Ready. Its first live run needs your OK (read-only SSH to the server).",
                         next_step="python -m office.jobs run crontab-guard --live")
    try:
        r = subprocess.run(ssh_command(cfg), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return JobResult(**base, status="ERROR", summary=f"Could not reach the server: {exc}", live=True)
    if r.returncode != 0:
        return JobResult(**base, status="ERROR", live=True,
                         summary=f"ssh exited {r.returncode}: {r.stderr.strip()[:200]}")
    status, findings = check_crontab(r.stdout, cfg["baseline_lines"], cfg["required_entries"])
    summary = ("Crontab intact." if status == "OK"
               else "Crontab changed. Check it before the next cron cycle.")
    return JobResult(**base, status=status, summary=summary, findings=findings, live=True,
                     next_step="" if status == "OK" else
                     "Compare with the canonical crontab; restore only via the append-only pattern.")


# ── social batch ──────────────────────────────────────────────────────────────

_FM_FIELD = re.compile(r"^(\w+):\s*(.*)$")


def read_knowledge_item(path: Path) -> dict | None:
    """Minimal front-matter reader (id, title, category, source, approval_status)."""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    meta = {}
    for line in text[3:end].splitlines():
        m = _FM_FIELD.match(line)
        if m:
            value = m.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            meta[m.group(1)] = value
    meta["path"] = path
    return meta


def pick_social_batch(repo: Path, count: int = 3) -> tuple[list[dict], int, int]:
    """Approved knowledge items not yet turned into a post, one per category first.

    Returns (picks, approved_total, used_total).
    """
    used = set()
    for record in (repo / "visuals" / "approved").glob("*.json"):
        try:
            data = json.loads(record.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            continue
        if isinstance(data, dict) and isinstance(data.get("source_knowledge_id"), str):
            used.add(data["source_knowledge_id"])
    approved = []
    for path in sorted((repo / "knowledge").rglob("*.md")):
        item = read_knowledge_item(path) if path.is_file() else None
        if (item and item.get("source") == "LUNNEYS_APPROVED"
                and item.get("approval_status") == "APPROVED"):
            approved.append(item)
    fresh = [i for i in approved if i.get("id") not in used]
    picks, seen_categories = [], set()
    if count < 1:
        return picks, len(approved), len(used & {i.get("id") for i in approved})
    for item in fresh:                      # one per category first
        if item.get("category") not in seen_categories:
            picks.append(item)
            seen_categories.add(item.get("category"))
        if len(picks) == count:
            break
    for item in fresh:                      # then fill up
        if len(picks) == count:
            break
        if item not in picks:
            picks.append(item)
    return picks, len(approved), len(used & {i.get("id") for i in approved})


def job_social_batch(args) -> JobResult:
    spec = JOBS["social-batch"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    repo = Path(args.social_repo)
    if not (repo / "knowledge").is_dir():
        return JobResult(**base, status="ERROR",
                         summary=f"Lunneys Social repo not found at {repo} (use --social-repo).")
    picks, approved, used = pick_social_batch(repo, args.count)
    if not picks:
        return JobResult(**base, status="OK",
                         summary=f"All {approved} approved knowledge items already have an approved visual.")
    findings = [f"{p.get('category')}: {p.get('title')}" for p in picks]
    commands = "; ".join(
        f'python knowledge_run.py "{p["path"].relative_to(repo).as_posix()}"' for p in picks)
    return JobResult(**base, status="READY",
                     summary=(f"{len(picks)} posts suggested from {approved - used} unused approved "
                              f"items ({used} already have an approved visual). You approve each at Gate 1 and Gate 2."),
                     findings=findings, next_step=f'cd "{repo}"; {commands}')


# ── category images (CCP 3) ───────────────────────────────────────────────────

CCP3_PHASES = [
    "Design: Gemini makes the artwork, Canva lays out one 1200x630 social image per "
    "top-level category. You approve them.",
    "Phase 2 (change): upload + set each category's image via REST, cache flush, "
    "targeted Cloudflare purge, then re-run this audit to confirm.",
]
LOGO_FALLBACK = "/media/logo/"
CCP3_FIELDS = ["group", "url", "verdict", "og_image", "status"]
_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _site_url(url: str | None) -> str | None:
    """The URL if it is an https page on this site, else None (never fetch anything else)."""
    if not url:
        return None
    url = url.strip()
    parts = urlsplit(url)
    return url if parts.scheme == "https" and parts.netloc == urlsplit(SITE).netloc else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(xml_text: str) -> tuple[list[str], list[str]]:
    """(category page URLs, child sitemap URLs) from a sitemap or sitemap index.

    Category pages are the <url> entries with priority 0.5 (Magento's default for
    categories). Only https URLs on this site are returned.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], []
    def loc_of(el):
        for child in el:
            if _local(child.tag) == "loc":
                return _site_url(child.text)
        return None
    if _local(root.tag) == "sitemapindex":
        return [], [u for u in (loc_of(el) for el in root if _local(el.tag) == "sitemap") if u]
    urls = []
    for el in root:
        if _local(el.tag) != "url":
            continue
        priority = next((c.text for c in el if _local(c.tag) == "priority"), None)
        try:
            is_category = priority is not None and float(priority) == 0.5
        except ValueError:
            is_category = False
        loc = loc_of(el)
        if is_category and loc:
            urls.append(loc)
    return urls, []


def category_urls(sitemap_xml: str) -> list[str]:
    return parse_sitemap(sitemap_xml)[0]


class _ShareTagParser(HTMLParser):
    """Collects og:* and twitter:* meta tags (first of each) and the <title>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: dict[str, str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag != "meta":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        key = (a.get("property") or a.get("name") or "").strip().lower()
        if key.startswith(("og:", "twitter:")) and key not in self.tags:
            self.tags[key] = a.get("content", "").strip()

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def share_tags(page: str) -> tuple[dict[str, str], str]:
    parser = _ShareTagParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:
        pass
    return parser.tags, " ".join(parser.title.split())


class _OgImageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "meta" or self.image is not None:
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if "og:image" in (a.get("property", "").strip().lower(), a.get("name", "").strip().lower()):
            self.image = a.get("content", "").strip()


def og_image(page: str) -> str | None:
    """The page's og:image (entities decoded), or None."""
    parser = _OgImageParser()
    try:
        parser.feed(page)
        parser.close()
    except Exception:  # malformed markup: use whatever was found
        pass
    return parser.image or None


def category_group(url: str) -> str:
    """Top-level group of a category URL: 'home-appliances', 'brands', ..."""
    path = urlsplit(url).path.lstrip("/")
    if path.startswith("catalog/category/view/"):
        return "no friendly URL"
    return path.split("/")[0].removesuffix(".html") or "(home)"


_BOT_CHECK = re.compile(r"<title>\s*One moment, please", re.I)


def classify_page(status, page: str) -> tuple[str, str]:
    """(verdict, og:image) for one fetched category page."""
    if status != 200:
        return f"HTTP {status}", ""
    if _BOT_CHECK.search(page[:5000]):
        return "bot check", ""
    image = og_image(page)
    if not image:
        return "no og:image", ""
    if not image.lower().startswith(("http://", "https://")):
        return "relative og:image", image
    if LOGO_FALLBACK in image:
        return "logo fallback", image
    return "has image", image


def audit_category_images(urls: list[str], fetch, delay: float = 0.5,
                          deadline: float | None = None, on_row=None,
                          bot_wait: float = 20.0) -> list[dict]:
    """Fetch each category page (one at a time) and record its og:image.

    Stops early, without error, once time.monotonic() passes `deadline`.
    """
    rows = []
    for i, url in enumerate(urls):
        if deadline is not None and time.monotonic() > deadline:
            break
        if i and delay:
            time.sleep(delay)
        try:
            status, page = fetch(url)
            verdict, image = classify_page(status, page)
            if verdict == "bot check":  # the host's firewall paused us: back off, retry once
                time.sleep(bot_wait)
                status, page = fetch(url)
                verdict, image = classify_page(status, page)
        except Exception as exc:  # one bad page must not stop the audit
            status, verdict, image = "error", f"error: {exc}"[:120], ""
        row = {"url": url, "group": category_group(url), "status": status,
               "og_image": image, "verdict": verdict}
        rows.append(row)
        if on_row:
            on_row(row)
    return rows


def _csv_safe(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_CSV_FORMULA_START) else text


def _md_cell(value) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _write_ccp3_markdown(path: Path, rows: list[dict], total: int) -> None:
    groups: dict[str, dict] = {}
    for r in rows:
        g = groups.setdefault(r["group"], {"total": 0, "missing": []})
        g["total"] += 1
        if r["verdict"] != "has image":
            g["missing"].append(r)
    missing = sum(len(g["missing"]) for g in groups.values())
    lines = ["# CCP 3 Phase 1: category social images", "",
             f"{missing} of {len(rows)} category pages checked do not share their own image "
             "(logo fallback, no or relative og:image, or not reachable)."]
    if len(rows) < total:
        lines.append(f"**Stopped early:** {len(rows)} of {total} pages checked (time limit).")
    lines += ["", "| Group | Pages | Missing an image |", "|---|---|---|"]
    for name, g in sorted(groups.items(), key=lambda kv: -len(kv[1]["missing"])):
        lines.append(f"| {_md_cell(name)} | {g['total']} | {len(g['missing'])} |")
    lines += ["", "## Pages missing an image", ""]
    for name, g in sorted(groups.items()):
        if g["missing"]:
            lines += [f"### {_md_cell(name)}", ""]
            lines += [f"- {_md_cell(r['url'])} ({_md_cell(r['verdict'])})" for r in g["missing"]]
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def job_category_images(args) -> JobResult:
    spec = JOBS["category-images"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    if not args.live:
        return JobResult(
            **base, status="NEEDS_YOU",
            summary=("CCP 3 is written and queued. Category pages without an image share the "
                     "store logo as their social image (e.g. /home-appliances.html)."),
            findings=["Phase 1 (read-only): audit every category page on the public site; "
                      "run with --live."] + CCP3_PHASES,
            next_step="python -m office.jobs run category-images --live")
    try:
        status, sitemap = _fetch(f"{SITE}/sitemap.xml", limit=20_000_000)
        urls, children = parse_sitemap(sitemap) if status == 200 else ([], [])
        for child in children[:20]:
            child_status, child_xml = _fetch(child, limit=20_000_000)
            if child_status == 200:
                urls += parse_sitemap(child_xml)[0]
    except Exception as exc:
        return JobResult(**base, status="ERROR", live=True, summary=f"Could not read the sitemap: {exc}")
    urls = list(dict.fromkeys(urls))
    if not urls:
        return JobResult(**base, status="ERROR", live=True,
                         summary=f"No category pages found in the sitemap (HTTP {status}).")
    intray = Path(args.intray)
    intray.mkdir(parents=True, exist_ok=True)
    csv_path, md_path = intray / "ccp3-audit.csv", intray / "ccp3-audit.md"
    import csv
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CCP3_FIELDS)
        writer.writeheader()

        def write_row(row):  # written as we go, so a stopped audit keeps its results
            writer.writerow({k: _csv_safe(row[k]) for k in CCP3_FIELDS})
            fh.flush()
        rows = audit_category_images(urls, _fetch, delay=args.delay,
                                     deadline=time.monotonic() + args.max_minutes * 60,
                                     on_row=write_row)
    _write_ccp3_markdown(md_path, rows, len(urls))
    missing = [r for r in rows if r["verdict"] != "has image"]
    by_group: dict[str, int] = {}
    for r in missing:
        by_group[r["group"]] = by_group.get(r["group"], 0) + 1
    findings = [f"{name}: {n} page(s)" for name, n in
                sorted(by_group.items(), key=lambda kv: -kv[1])[:12]]
    if len(rows) < len(urls):
        findings.insert(0, f"Stopped early at the {args.max_minutes}-minute limit: "
                           f"{len(rows)} of {len(urls)} pages checked.")
    findings = [f"Report: {md_path}", f"Full list: {csv_path}"] + findings + CCP3_PHASES
    return JobResult(
        **base, live=True, status="NEEDS_YOU" if missing or len(rows) < len(urls) else "OK",
        summary=(f"Phase 1 done: {len(missing)} of {len(rows)} category pages share no image of "
                 f"their own, across {len(by_group)} top-level group(s)."
                 if missing else f"All {len(rows)} category pages checked share their own image."),
        findings=findings,
        next_step=("Decide which groups get artwork first; Phase 2 needs the images and your "
                   "approval for the live changes." if missing else ""))


# ── product copy check ────────────────────────────────────────────────────────

# Words that describe the same thing in different ways: a meta description that uses one
# word of a group while the product's own name/text uses a different one is a contradiction.
TYPE_GROUPS = {
    "vacuum type": {"upright": "upright", "cylinder": "cylinder", "canister": "cylinder",
                    "cordless": "cordless", "stick": "cordless", "robot": "robot",
                    "handheld": "handheld"},
    "power": {"cordless": "cordless", "corded": "corded"},
    "installation": {"freestanding": "freestanding", "free-standing": "freestanding",
                     "integrated": "built-in", "built-in": "built-in", "built in": "built-in"},
}
PROMISES = ["free delivery", "free installation", "free fitting", "next day", "next-day",
            "same day", "price match", "lowest price", "best price", "cheapest", "guaranteed"]
_TAGS = re.compile(r"<[^>]+>")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def repeated_phrase(name: str) -> str | None:
    """A run of two or more words that appears twice in the name, e.g. 'dyson big ball'."""
    words = _words(name)
    for n in range(min(5, len(words) // 2), 1, -1):
        seen = set()
        for i in range(len(words) - n + 1):
            gram = tuple(words[i:i + n])
            if gram in seen and not all(w.isdigit() for w in gram):
                return " ".join(gram)
            seen.add(gram)
    return None


def _terms(text: str, group: dict) -> set[str]:
    low = " " + " ".join(text.lower().split()) + " "
    return {canon for word, canon in group.items()
            if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z-])", low)}


def _is_euronics(product: dict) -> bool:
    """Local Euronics items are the only products without free delivery (Craig, 2026-10-16)."""
    return str(product.get("is_euronics") or "").strip().lower() in ("1", "true", "yes")


def check_product_copy(product: dict) -> list[tuple[str, str]]:
    """[(issue, detail)] for one product: name, meta_title, meta_description, description."""
    issues = []
    name = product.get("name") or ""
    meta = (product.get("meta_description") or "").strip()
    own = name + " " + _TAGS.sub(" ", (product.get("description") or "") + " "
                                  + (product.get("short_description") or ""))
    phrase = repeated_phrase(name)
    if phrase:
        issues.append(("name repeats words", f"'{phrase}' appears twice"))
    if not meta:
        issues.append(("no meta description", "Google and shares fall back to other text"))
    else:
        if len(meta) > 160:
            issues.append(("meta description long", f"{len(meta)} characters (about 155 shows)"))
        for label, group in TYPE_GROUPS.items():
            said, actual = _terms(meta, group), _terms(own, group)
            if said and actual and not said & actual:
                issues.append((f"{label} contradiction",
                               f"meta says {', '.join(sorted(said))}; product says {', '.join(sorted(actual))}"))
        low = meta.lower()
        promised = [p for p in PROMISES if p in low and p not in own.lower()
                    and not (p == "free delivery" and not _is_euronics(product))]
        if promised:
            issues.append(("promise to check", "meta says: " + ", ".join(promised)))
    if "lunneys" in (product.get("meta_title") or "").lower():
        issues.append(("meta title repeats Lunneys", "the site adds ' | Lunneys' itself"))
    return issues


def load_api_config(path: Path) -> dict:
    """Base URL and token from the Lunneys scripts config named in the office config."""
    import configparser
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"could not read {path}: {exc}") from None
    ini = (data.get("magento_api") or {}).get("config_ini") if isinstance(data, dict) else None
    if not isinstance(ini, str) or not ini:
        raise ConfigError(f'{path} has no "magento_api": {{"config_ini": ...}} setting')
    parser = configparser.ConfigParser()
    if not parser.read(ini, encoding="utf-8") or "magento" not in parser:
        raise ConfigError(f"no [magento] section in {ini}")
    base, token = parser["magento"].get("base_url", "").rstrip("/"), parser["magento"].get("token", "").strip()
    if not SHARE_URL.fullmatch(base) or not token:
        raise ConfigError("the [magento] base_url must be https://loshop.co.uk and a token must be set")
    return {"base": base, "token": token}


def _api_get(api: dict, path: str) -> dict:
    req = urllib.request.Request(api["base"] + path, headers={
        "Authorization": "Bearer " + api["token"], "User-Agent": "AgentOffice/1.0 (read-only check)"})
    with _OPENER.open(req, timeout=120) as resp:  # noqa: S310 - site URL only
        return json.loads(resp.read())


def fetch_products(api: dict, page_size: int = 200, get=None) -> list[dict]:
    """Every enabled product (status=1): sku, name, url_key and the copy attributes."""
    get = get or (lambda path: _api_get(api, path))
    fields = ("items[sku,name,status,visibility,custom_attributes[attribute_code,value]],"
              "total_count")
    products, page = [], 1
    while True:
        query = urllib.parse.urlencode({
            "searchCriteria[filterGroups][0][filters][0][field]": "status",
            "searchCriteria[filterGroups][0][filters][0][value]": "1",
            "searchCriteria[pageSize]": page_size, "searchCriteria[currentPage]": page,
            "fields": fields})
        data = get(f"/rest/all/V1/products?{query}")
        items = data.get("items") or []
        for item in items:
            ca = {a.get("attribute_code"): a.get("value") for a in item.get("custom_attributes") or []}
            products.append({"sku": item.get("sku"), "name": item.get("name"),
                             "visibility": item.get("visibility"), **{k: ca.get(k) for k in (
                                 "url_key", "meta_title", "meta_description", "description",
                                 "short_description", "is_euronics")}})
        if not items or len(products) >= int(data.get("total_count") or 0) or page >= 100:
            return products
        page += 1


def job_product_copy(args) -> JobResult:
    spec = JOBS["product-copy"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    try:
        api = load_api_config(Path(args.config))
    except ConfigError as exc:
        return JobResult(**base, status="NEEDS_YOU", summary=f"Not configured: {exc}.",
                         next_step='Add "magento_api": {"config_ini": "<path to lunneys-scripts '
                                   'config.ini>"} to ~/.agent-office/config.json.')
    if not args.live:
        return JobResult(**base, status="NEEDS_YOU", summary="Ready: run it live (read-only API).",
                         next_step="python -m office.jobs run product-copy --live")
    try:
        products = fetch_products(api)
    except Exception as exc:
        return JobResult(**base, status="ERROR", live=True,
                         summary=f"Could not read products: {str(exc)[:200]}")
    rows = []
    for p in products:
        if p.get("visibility") == 1:  # not visible on the site: skip
            continue
        for issue, detail in check_product_copy(p):
            rows.append({"issue": issue, "sku": p.get("sku"), "name": p.get("name"),
                         "url": f"{SITE}/{p['url_key']}.html" if p.get("url_key") else "",
                         "detail": detail, "meta_description": p.get("meta_description") or ""})
    intray = Path(args.intray)
    intray.mkdir(parents=True, exist_ok=True)
    csv_path = intray / "product-copy.csv"
    import csv
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["issue", "sku", "name", "url", "detail",
                                                "meta_description"])
        writer.writeheader()
        for r in sorted(rows, key=lambda r: (r["issue"], str(r["name"]))):
            writer.writerow({k: _csv_safe(v) for k, v in r.items()})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["issue"]] = counts.get(r["issue"], 0) + 1
    order = ["vacuum type contradiction", "power contradiction", "installation contradiction",
             "promise to check", "name repeats words", "no meta description",
             "meta description long", "meta title repeats Lunneys"]
    findings = [f"{k}: {counts[k]}" for k in order if k in counts]
    examples = [r for r in rows if "contradiction" in r["issue"] or r["issue"] == "promise to check"][:5]
    findings += [f"e.g. {r['name']}: {r['detail']}" for r in examples]
    findings.append(f"Full list: {csv_path}")
    checked = sum(1 for p in products if p.get("visibility") != 1)
    return JobResult(**base, live=True, status="NEEDS_YOU" if rows else "OK", findings=findings,
                     summary=(f"{len({r['sku'] for r in rows})} of {checked} products have copy to check."
                              if rows else f"All {checked} products' copy looks consistent."),
                     next_step="Open the CSV, fix the worst first (contradictions, promises)."
                     if rows else "")


# ── share preview ─────────────────────────────────────────────────────────────

SHARE_URL = re.compile(r"https://loshop\.co\.uk(?:/[^\s<>\"'\\]{0,400})?")
DEFAULT_SHARE_IMAGE = "/media/logo/stores/1/logo.png"


def _fetch_bytes(url: str, limit: int = 8_000_000) -> tuple[int, str, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "AgentOffice/1.0 (read-only check)"})
    try:
        with _OPENER.open(req, timeout=30) as resp:  # noqa: S310 - site URLs only
            return resp.status, resp.headers.get("Content-Type", ""), resp.read(limit)
    except urllib.error.HTTPError as exc:
        return exc.code, "", b""


def image_size(data: bytes) -> tuple[int, int] | None:
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def check_share_tags(tags: dict, image: dict | None) -> tuple[str, list[str]]:
    """(status, findings) for a page's share tags and its fetched image."""
    findings, status = [], "OK"

    def flag(level, text):
        nonlocal status
        findings.append(text)
        if level == "ALERT" or status == "OK":
            status = level
    if not tags.get("og:title"):
        flag("ALERT", "No og:title: the shared link has no headline.")
    desc = tags.get("og:description", "")
    if not desc:
        flag("NEEDS_YOU", "No og:description: the shared link has no summary line.")
    elif len(desc) > 300:
        flag("NEEDS_YOU", f"og:description is {len(desc)} characters; under 200 reads best.")
    url = tags.get("og:image", "")
    if not url:
        flag("ALERT", "No og:image: Facebook and WhatsApp will show no picture, or a random one.")
    elif not url.startswith("https://"):
        flag("ALERT", "og:image is not a full https:// address, so platforms cannot load it.")
    elif image and image.get("status") == "off-site":
        flag("NEEDS_YOU", "The share image is on another website, so it was not checked.")
    elif image is None or image.get("status") != 200:
        flag("ALERT", f"The share image does not load (HTTP {image and image.get('status')}).")
    elif not str(image.get("type", "")).lower().startswith("image/"):
        flag("ALERT", f"The share image address does not return an image ({image.get('type') or 'no type'}).")
    else:
        size = image.get("size")
        if not size:
            flag("NEEDS_YOU", "The share image could not be read (damaged, unusual format or too large).")
        else:
            w, h = size
            findings.append(f"Image {w}x{h}, {image['bytes'] // 1024} KB.")
            if w < 600:
                flag("NEEDS_YOU", "The image is under 600 pixels wide: Facebook shows it as a small "
                                  "thumbnail. 1200x630 is ideal.")
            elif not 1.5 <= w / h <= 2.2:
                flag("NEEDS_YOU", "The image is not wide (about 1.91:1), so platforms will crop it.")
        if DEFAULT_SHARE_IMAGE in url:
            findings.append("Uses the general Lunneys share image (this page has no picture of its own).")
    if tags.get("twitter:card") not in (None, "summary_large_image"):
        findings.append(f"twitter:card is {tags.get('twitter:card')!r}: X shows a small preview, "
                        "not the large image.")
    return status, findings


def job_share_preview(args) -> JobResult:
    spec = JOBS["share-preview"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    url = (args.url or "").strip()
    if not SHARE_URL.fullmatch(url):
        return JobResult(**base, status="ERROR",
                         summary="Give a link on https://loshop.co.uk/ (other sites are not checked).")
    if not args.live:
        return JobResult(**base, status="NEEDS_YOU", summary="Ready: run it live to read the page.",
                         next_step=f"python -m office.jobs run share-preview --live --url {url}")
    try:
        status_code, page = _fetch(url, limit=2_000_000)
    except Exception as exc:
        return JobResult(**base, status="ERROR", live=True, summary=f"Could not read the page: {exc}")
    if status_code != 200:
        return JobResult(**base, status="ERROR", live=True, summary=f"The page answered HTTP {status_code}.")
    if _BOT_CHECK.search(page[:5000]):
        return JobResult(**base, status="ERROR", live=True,
                         summary="The host's bot check answered instead of the page; try again in a few minutes.")
    tags, page_title = share_tags(page)
    image = None
    img_url = tags.get("og:image", "")
    if img_url.startswith("https://") and not SHARE_URL.fullmatch(img_url):
        image = {"status": "off-site"}
    elif img_url.startswith("https://"):
        try:
            img_status, ctype, data = _fetch_bytes(img_url)
            image = {"status": img_status, "type": ctype, "bytes": len(data), "size": image_size(data)}
        except Exception as exc:
            image = {"status": f"error ({exc})"}
    status, findings = check_share_tags(tags, image)
    preview = {"url": url, "site": "LOSHOP.CO.UK", "title": tags.get("og:title") or page_title,
               "description": tags.get("og:description", ""), "image": img_url}
    return JobResult(**base, status=status, live=True, findings=findings, preview=preview,
                     summary=(f"Looks good when shared: {preview['title']}" if status == "OK"
                              else f"Share preview needs attention: {preview['title'] or url}"))


# ── Reviews.io replies ────────────────────────────────────────────────────────

REPLY_RULES = [
    "Thank the customer by first name; mention the product or store they mention.",
    "Never promise refunds, prices, delivery dates or anything not already agreed.",
    "No invented facts: only what is in the review.",
    "3 stars or below: do not draft a public reply; flag to the boss with a suggested next step.",
    "Sign off as the Lunneys team (Banbridge & Portadown); keep it under 60 words.",
]


def _quote(text) -> str:
    """Markdown block quote of every line, so review text cannot add headings."""
    return "\n".join(f"> {line}" for line in str(text).splitlines() or [""])


def _one_line(text) -> str:
    """Collapse whitespace so a name or product cannot start a new Markdown line."""
    return " ".join(str(text).split())


def build_reply_pack(reviews: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split reviews into (to_draft, to_boss). Each review: name, rating, text, [product]."""
    to_draft, to_boss = [], []
    for r in reviews:
        try:
            rating = int(r.get("rating", 0))
        except (TypeError, ValueError):
            rating = 0
        (to_draft if rating >= 4 else to_boss).append({**r, "rating": rating})
    return to_draft, to_boss


def job_reviews_replies(args) -> JobResult:
    spec = JOBS["reviews-replies"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    if not args.input:
        return JobResult(**base, status="NEEDS_CONNECTION",
                         summary=("Needs reviews to work on: export recent reviews from Reviews.io "
                                  "as JSON (name, rating, text, product), or connect Reviews.io."),
                         findings=REPLY_RULES,
                         next_step="python -m office.jobs run reviews-replies --input reviews.json")
    try:
        reviews = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return JobResult(**base, status="ERROR", summary=f"Could not read {args.input}: {exc}")
    if not isinstance(reviews, list) or not all(isinstance(r, dict) for r in reviews):
        return JobResult(**base, status="ERROR",
                         summary="Expected a JSON list of reviews: [{name, rating, text, product}, ...]")
    to_draft, to_boss = build_reply_pack(reviews)
    pack = Path(args.intray) / "reviews-reply-brief.md"
    lines = ["# Reviews.io reply brief", "", "## Rules", *[f"- {r}" for r in REPLY_RULES], "",
             "## Draft replies for these", ""]
    for r in to_draft:
        lines += [f"### {_one_line(r.get('name', 'Customer'))} · {r['rating']} stars"
                  + (f" · {_one_line(r['product'])}" if r.get("product") else ""), "", _quote(r.get("text", "")), "",
                  "Reply: _(draft here)_", ""]
    lines += ["## For the boss first (3 stars or below)", ""]
    for r in to_boss:
        lines += [f"- **{_one_line(r.get('name', 'Customer'))}**, {r['rating']} stars:", "",
                  _quote(r.get("text", "")), ""]
    pack.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return JobResult(**base, status="READY" if not to_boss else "NEEDS_YOU",
                     summary=(f"{len(to_draft)} replies to draft; {len(to_boss)} low-rated review(s) "
                              "for you first. Nothing is posted."),
                     findings=[f"Brief: {pack}"],
                     next_step="Hand the brief to ChatGPT or Claude to draft; you approve and post.")


# ── morning briefing ──────────────────────────────────────────────────────────

class _SiteOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https://loshop.co.uk; anything else becomes an HTTP error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not SHARE_URL.fullmatch(newurl):
            raise urllib.error.HTTPError(newurl, code, "redirect off this site refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_SiteOnlyRedirects)


def _fetch(url: str, limit: int = 200_000) -> tuple[int, str]:
    """GET a public page: (status, first `limit` bytes as text). HTTP errors are statuses."""
    req = urllib.request.Request(url, headers={"User-Agent": "AgentOffice/1.0 (read-only check)"})
    try:
        with _OPENER.open(req, timeout=30) as resp:  # noqa: S310 - site URLs only
            return resp.status, resp.read(limit).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def check_robots(text: str) -> list[str]:
    """Problems with loshop's robots.txt, per the hard rules in the SEO guide."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    problems = []
    for needle, why in [
        ("User-agent: Googlebot", "explicit Googlebot rule missing"),
        ("User-agent: Googlebot-image", "explicit Googlebot-image rule missing"),
        ("Sitemap: https://loshop.co.uk/sitemap.xml", "apex sitemap line missing"),
        ("User-agent: GPTBot", "AI crawler policy block missing"),
    ]:
        if not re.search(rf"^{re.escape(needle)}[ 	]*$", text, flags=re.M | re.I):
            problems.append(why)
    if re.search(r"^Disallow:\s*/\*[?&]", text, flags=re.M | re.I):
        problems.append("a query-string Disallow would block layered navigation")
    return problems


def job_morning_briefing(args) -> JobResult:
    spec = JOBS["morning-briefing"]
    base = dict(job=spec.name, title=spec.title, desk=spec.desk, level=spec.level)
    intray = Path(args.intray)
    findings: list[str] = []
    alert = False
    try:
        from .dashboard import snapshot
        snap = snapshot(Path(args.relay_db), Path(args.a2a_db))
        findings.append(f"Waiting on you in Relay/A2A: {len(snap.waiting)}")
        findings += [f"  - {w['what']}: {w['id']}" for w in snap.waiting[:8]]
    except Exception as exc:  # the briefing must still be produced
        findings.append(f"Office databases not readable: {exc}")
    retired = retired_jobs(Path(args.config))
    for name in JOBS:
        if name in ("morning-briefing", "share-preview") or name in retired:
            continue
        result = load_result(intray, name)
        if result is None:
            findings.append(f"{JOBS[name].title}: not run yet")
            continue
        status = str(result.get("status", "?"))
        alert = alert or status in ("ALERT", "ERROR")
        when = uk_time(result.get("generated_at", ""))
        findings.append(f"{JOBS[name].title}: {status}: {result.get('summary', '')}"
                        + (f" (as of {when})" if when else ""))
    if args.live:
        try:
            status, text = _fetch(f"{SITE}/robots.txt")
            problems = check_robots(text) if status == 200 else [f"HTTP {status}"]
            findings.append("robots.txt: " + ("intact" if not problems else "; ".join(problems)))
            status, body = _fetch(f"{SITE}/sitemap.xml")
            sitemap_ok = status == 200 and ("<urlset" in body or "<sitemapindex" in body)
            findings.append(f"sitemap.xml: HTTP {status}" + ("" if sitemap_ok else " (not a sitemap)"))
            alert = alert or bool(problems) or not sitemap_ok
        except Exception as exc:
            findings.append(f"Website checks failed: {exc}")
            alert = True
    else:
        findings.append("Website checks skipped (run with --live to include robots.txt and sitemap).")
    findings.append("Base.com orders and courier exceptions: not connected yet (your decision).")
    note = intray / f"morning-briefing-{datetime.now().strftime('%Y-%m-%d')}.md"
    note.write_text("# Morning briefing\n\n" + "\n".join(f"- {f}" if not f.startswith("  ")
                                                         else f for f in findings) + "\n",
                    encoding="utf-8")
    return JobResult(**base, status="ALERT" if alert else "OK", live=args.live,
                     summary=f"Briefing written: {note.name}", findings=findings)


# ── plumbing ──────────────────────────────────────────────────────────────────

RUNNERS = {
    "backup-check": job_backup_check,
    "share-preview": job_share_preview,
    "product-copy": job_product_copy,
    "crontab-guard": job_crontab_guard,
    "morning-briefing": job_morning_briefing,
    "social-batch": job_social_batch,
    "category-images": job_category_images,
    "reviews-replies": job_reviews_replies,
}


def save_result(intray: Path, result: JobResult) -> Path:
    intray.mkdir(parents=True, exist_ok=True)
    path = intray / f"{result.job}.json"
    path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
    return path


def load_result(intray: Path, job: str) -> dict | None:
    path = Path(intray) / f"{job}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="office.jobs", description="Agent Office in-tray jobs")
    parser.add_argument("--intray", default=str(DEFAULT_INTRAY), help="in-tray folder")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="local settings file (server details; never in the repository)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the jobs")
    run = sub.add_parser("run", help="run one job")
    run.add_argument("job", choices=sorted(RUNNERS))
    run.add_argument("--live", action="store_true",
                     help="allow read-only checks of the live server/website")
    run.add_argument("--social-repo", default=str(DEFAULT_SOCIAL_REPO))
    run.add_argument("--count", type=_positive_int, default=3)
    run.add_argument("--input", default=None, help="reviews JSON for reviews-replies")
    run.add_argument("--url", default=None, help="the loshop.co.uk link for share-preview")
    run.add_argument("--delay", type=float, default=2.0,
                     help="seconds between page fetches in the category-images audit")
    run.add_argument("--max-minutes", type=_positive_int, default=50,
                     help="time limit for the category-images audit")
    run.add_argument("--relay-db", default="relay.db")
    run.add_argument("--a2a-db", default="a2a.db")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if args.command == "list":
        retired = retired_jobs(Path(args.config))
        for spec in JOBS.values():
            if spec.name in retired:
                print(f"{spec.name:18} (retired)")
                continue
            live = " (live read needs --live)" if spec.live_needed else ""
            print(f"{spec.name:18} {spec.level:7} {spec.desk}: {spec.blurb}{live}")
        return 0
    Path(args.intray).mkdir(parents=True, exist_ok=True)
    result = RUNNERS[args.job](args)
    path = save_result(Path(args.intray), result)
    print(f"[{result.status}] {result.title}: {result.summary}")
    for f in result.findings:
        print(f"  - {f}")
    if result.next_step:
        print(f"Next: {result.next_step}")
    print(f"(saved to {path})")
    return 1 if result.status in ("ALERT", "ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
