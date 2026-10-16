"""
Share-preview checker, backup check and Windows alerts.

No test reaches the live site, the server or the Windows notification system:
fetches, SSH and PowerShell are all replaced.
"""
from __future__ import annotations

import base64
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from office import jobs, notify
from office.dashboard import render, snapshot

PAGE = """<html><head><title>Home Appliances | Lunneys</title>
<meta property="og:title" content="Home Appliances">
<meta property="og:description" content="Shop home appliances &amp; more.">
<meta property="og:image" content="https://loshop.co.uk/media/logo/stores/1/logo.png">
<meta name="twitter:card" content="summary_large_image"></head><body></body></html>"""


def _png(w, h):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def no_live_access(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"unexpected live access: {a!r}")
    monkeypatch.setattr(jobs, "subprocess", SimpleNamespace(
        run=refuse, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(jobs, "_fetch", refuse)
    monkeypatch.setattr(jobs, "_fetch_bytes", refuse)


def _run(tmp_path, *argv, config=None):
    intray = tmp_path / "intray"
    code = jobs.main(["--intray", str(intray), "--config", str(config or tmp_path / "none.json"), *argv])
    return code, json.loads((intray / f"{argv[1]}.json").read_text(encoding="utf-8")), intray


# ── share preview ─────────────────────────────────────────────────────────────

def test_share_tags_parsing():
    tags, title = jobs.share_tags(PAGE)
    assert title == "Home Appliances | Lunneys"
    assert tags["og:description"] == "Shop home appliances & more."
    assert tags["twitter:card"] == "summary_large_image"


@pytest.mark.parametrize("url", ["https://evil.example/", "http://loshop.co.uk/", "file:///C:/x",
                                 "https://loshop.co.uk.evil.example/", "https://loshop.co.uk/a b"])
def test_share_preview_only_checks_this_site(tmp_path, url):
    code, result, _ = _run(tmp_path, "run", "share-preview", "--live", "--url", url)
    assert code == 1 and result["status"] == "ERROR"


def test_share_preview_good_page(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=0: (200, PAGE))
    monkeypatch.setattr(jobs, "_fetch_bytes", lambda url, limit=0: (200, "image/png", _png(1200, 630)))
    code, result, intray = _run(tmp_path, "run", "share-preview", "--live",
                                "--url", "https://loshop.co.uk/home-appliances.html")
    assert code == 0 and result["status"] == "OK"
    assert result["preview"]["image"].endswith("/logo.png")
    assert any("general Lunneys share image" in f for f in result["findings"])
    page = render(snapshot(tmp_path / "r", tmp_path / "a", intray), tmp_path / "r", tmp_path / "a")
    assert '<img src="https://loshop.co.uk/media/logo/stores/1/logo.png"' in page


@pytest.mark.parametrize("page,image,status,needle", [
    (PAGE.replace('<meta property="og:image" content="https://loshop.co.uk/media/logo/stores/1/logo.png">', ""),
     None, "ALERT", "No og:image"),
    (PAGE, (404, "", b""), "ALERT", "does not load"),
    (PAGE, (200, "image/png", None), "NEEDS_YOU", "under 600"),
    (PAGE, (200, "image/png", "square"), "NEEDS_YOU", "not wide"),
    (PAGE.replace('content="Home Appliances"', 'content=""'), (200, "image/png", None), "ALERT", "No og:title"),
])
def test_share_preview_problems(tmp_path, monkeypatch, page, image, status, needle):
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=0: (200, page))
    if image is not None:
        data = image[2]
        data = _png(400, 210) if data is None else (_png(800, 800) if data == "square" else data)
        monkeypatch.setattr(jobs, "_fetch_bytes", lambda url, limit=0: (image[0], image[1], data))
    _, result, _ = _run(tmp_path, "run", "share-preview", "--live", "--url", "https://loshop.co.uk/x.html")
    assert result["status"] == status and any(needle in f for f in result["findings"]), result


def test_share_card_only_shows_site_images(tmp_path):
    intray = tmp_path / "intray"
    jobs.save_result(intray, jobs.JobResult(
        job="share-preview", title="Share preview", desk="d", level="REPORT", status="OK",
        summary="s", preview={"url": "u", "site": "S", "title": "<b>T</b>", "description": "D",
                              "image": "https://evil.example/x.png"}))
    page = render(snapshot(tmp_path / "r", tmp_path / "a", intray), tmp_path / "r", tmp_path / "a")
    assert "evil.example" not in page and "no share image" in page and "&lt;b&gt;T&lt;/b&gt;" in page


# ── backup check ──────────────────────────────────────────────────────────────

GUARD = {"ssh_target": "shopuser@shop.example", "ssh_port": 2222, "ssh_options": []}


def _config(tmp_path, **backup):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"crontab_guard": GUARD, "backup_check": {
        "glob": "/home/shopuser/backups/db_backup_*.sql", **backup}}), encoding="utf-8")
    return path


NOW = 1_800_000_000


def _listing(*files):
    return "".join(f"{NOW - int(age * 86400)} {int(mb * 1_048_576)} /home/shopuser/backups/{n}\n"
                   for n, age, mb in files)


def test_backups_fine():
    status, findings = jobs.check_backups(_listing(("old.sql", 8, 39), ("new.sql", 1, 39.2)), NOW, 7, 20)
    assert status == "OK" and findings[0].startswith("Newest: new.sql") and "1.0 days old" in findings[0]


@pytest.mark.parametrize("listing,needle", [
    ("", "No full database copies"),
    (_listing(("a.sql", 9, 39)), "older than 7 days"),
    (_listing(("a.sql", 1, 5)), "under 20 MB"),
    (_listing(("a.sql", 3, 39), ("b.sql", 1, 20)), "much smaller"),
])
def test_backup_problems(listing, needle):
    status, findings = jobs.check_backups(listing, NOW, 7, 20)
    assert status == "ALERT" and any(needle in f for f in findings), findings


@pytest.mark.parametrize("glob", ["relative/*.sql", "/home/x/../etc/*", "/home/x/*.sql; rm -rf /",
                                  "/home/x/$(id)", "/home/x/`id`"])
def test_backup_glob_must_be_safe(tmp_path, glob):
    with pytest.raises(jobs.ConfigError, match="glob"):
        jobs.load_backup_config(_config(tmp_path, glob=glob))


def test_backup_check_live_runs_one_read_only_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_listing(("a.sql", 1, 39)), stderr="")
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    monkeypatch.setattr(jobs.time, "time", lambda: NOW)
    code, result, _ = _run(tmp_path, "run", "backup-check", "--live", config=_config(tmp_path))
    assert code == 0 and result["status"] == "OK"
    assert calls == [["ssh", "-p", "2222", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                      "shopuser@shop.example",
                      "stat -c '%Y %s %n' /home/shopuser/backups/db_backup_*.sql 2>/dev/null "
                      "| sort -n | tail -2"]]


def test_backup_check_needs_config(tmp_path):
    _, result, _ = _run(tmp_path, "run", "backup-check", "--live")
    assert result["status"] == "NEEDS_YOU" and "Not configured" in result["summary"]


# ── Windows alerts ────────────────────────────────────────────────────────────

def _results(intray, **statuses):
    for name, status in statuses.items():
        name = name.replace("_", "-")
        jobs.save_result(intray, jobs.JobResult(job=name, title=jobs.JOBS[name].title, desk="d",
                                                level="REPORT", status=status, summary="s"))


def test_alerts_only_for_problems_and_not_retired(tmp_path):
    intray = tmp_path / "i"
    _results(intray, crontab_guard="ALERT", backup_check="ERROR", social_batch="READY",
             category_images="ALERT")
    found = notify.problems(intray, frozenset({"category-images"}))
    assert [p["title"] for p in found] == ["Backup check", "Crontab guard"]


def test_quiet_morning_shows_nothing(tmp_path):
    intray = tmp_path / "i"
    _results(intray, crontab_guard="OK")
    calls = []
    assert notify.main(["--intray", str(intray), "--config", str(tmp_path / "c")],
                       run=lambda *a, **k: calls.append(a)) == 0
    assert calls == []


def test_alert_runs_powershell_with_an_encoded_script(tmp_path):
    intray = tmp_path / "i"
    _results(intray, crontab_guard="ALERT")
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0)
    notify.main(["--intray", str(intray), "--config", str(tmp_path / "c")], run=fake_run)
    argv = calls[0]
    assert argv[0].lower().endswith("powershell.exe") and argv[-2] == "-EncodedCommand"
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    inner = base64.b64decode(script.split("FromBase64String('")[1].split("'")[0]).decode("utf-8")
    assert "1 job(s) need a look" in inner and "Crontab guard: ALERT" in inner


@pytest.mark.parametrize("body", ["it's <b>&</b>' ; Remove-Item C:\\ ",
                                  "x\u2019) ; Write-Output PWNED ; $null=(\u2019",
                                  "\u201c\u201d\u2018\u201b`$(whoami)"])
def test_toast_text_cannot_break_out(body):
    script = notify.toast_script("Title", body)
    for bad in ("PWNED", "Remove-Item", "whoami", "\u2019", "\u2018", "\u201b"):
        assert bad not in script, bad
    encoded = script.split("FromBase64String('")[1].split("'")[0]
    xml = base64.b64decode(encoded).decode("utf-8")
    assert "&lt;b&gt;" in xml or "PWNED" in xml or "whoami" in xml


def test_one_off_link_checks_never_alert(tmp_path):
    intray = tmp_path / "i"
    jobs.save_result(intray, jobs.JobResult(job="share-preview", title="Share preview", desk="d",
                                            level="REPORT", status="ALERT", summary="s"))
    assert notify.problems(intray) == []


def test_off_site_share_image_is_not_fetched(tmp_path, monkeypatch):
    page = PAGE.replace("https://loshop.co.uk/media/logo/stores/1/logo.png", "https://evil.example/x.png")
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=0: (200, page))
    _, result, _ = _run(tmp_path, "run", "share-preview", "--live", "--url", "https://loshop.co.uk/a.html")
    assert result["status"] == "NEEDS_YOU" and any("another website" in f for f in result["findings"])


@pytest.mark.parametrize("ctype,data,status,needle", [
    ("text/html", b"<html>error</html>", "ALERT", "does not return an image"),
    ("image/png", b"not really a png", "NEEDS_YOU", "could not be read"),
])
def test_unreadable_share_image_is_flagged(tmp_path, monkeypatch, ctype, data, status, needle):
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=0: (200, PAGE))
    monkeypatch.setattr(jobs, "_fetch_bytes", lambda url, limit=0: (200, ctype, data))
    _, result, _ = _run(tmp_path, "run", "share-preview", "--live", "--url", "https://loshop.co.uk/a.html")
    assert result["status"] == status and any(needle in f for f in result["findings"])


def test_redirects_off_the_site_are_refused():
    import urllib.request
    handler = jobs._SiteOnlyRedirects()
    req = urllib.request.Request("https://loshop.co.uk/a")
    for target in ("http://localhost:8080/admin", "https://evil.example/", "ftp://x/y",
                   "http://loshop.co.uk/a"):
        with pytest.raises(jobs.urllib.error.HTTPError):
            handler.redirect_request(req, None, 302, "Found", {}, target)
    assert handler.redirect_request(req, None, 301, "Moved", {}, "https://loshop.co.uk/b") is not None


@pytest.mark.parametrize("listing,status,needle", [
    ("\u00b2 5 /x/a.sql\n", "ALERT", "No full database copies"),
    ("99999999999999 41943040 /x/a.sql\n", "ALERT", "No full database copies"),
    (f"{NOW + 30 * 86400} 41943040 /x/a.sql\n", "ALERT", "future"),
])
def test_odd_backup_listings_do_not_crash(listing, status, needle):
    got, findings = jobs.check_backups(listing, NOW, 7, 20)
    assert got == status and any(needle in f for f in findings), findings
