"""
Batch 7 — the Agent Office morning in-tray jobs (python -m office.jobs).

Nothing here may reach the live server or website: subprocess.run and the web
fetch inside office.jobs are replaced for every test, and a test that needs
them installs a fake that records the call.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from office import jobs

REAL_FETCH = jobs._fetch  # kept before the autouse fixture replaces it
from office.dashboard import render, snapshot

ROOT = Path(__file__).parent.parent

ROBOTS_OK = """# robots.txt for loshop.co.uk
User-agent: GPTBot
Allow: /

User-agent: Googlebot
Disallow:

User-agent: Googlebot-image
Disallow:

User-agent: *
Disallow: /admin/

Sitemap: https://loshop.co.uk/sitemap.xml
"""


REQUIRED = {"Magento cron": "bin/magento cron:run", "Probe": "probe.sh",
            "Publisher": "publish.py", "Stock sync": "stock_sync"}
GUARD = {"ssh_target": "shopuser@shop.example", "ssh_port": 2222,
         "ssh_options": ["MACs=hmac-sha2-256"], "baseline_lines": 29, "required_entries": REQUIRED}


def _crontab(lines=29, drop=None, comment=None):
    """A crontab with every required entry plus filler, `lines` long."""
    rows = []
    for label, needle in REQUIRED.items():
        if label == drop:
            continue
        row = f"*/15 * * * * /home/shopuser/run {needle}"
        rows.append(("# " + row) if label == comment else row)
    rows += [f"*/5 * * * * /home/shopuser/job{i}.sh" for i in range(max(0, lines - len(rows)))]
    return "\n".join(rows[:lines]) + "\n"


# A realistic 29-line crontab for a Magento shop on shared hosting (made-up paths).
SAMPLE_CRONTAB = """MAILTO=""
SHELL="/bin/bash"
0 3 * * 1 /usr/local/bin/php /home/shopuser/shop/generate_llms.php
SHELL="/bin/bash"
* * * * * /usr/local/bin/php /home/shopuser/shop/bin/magento cron:run >> var/log/magento.cron.log 2>&1
0 8 * * * curl -s "https://shop.example/notify" > /dev/null 2>&1
0 6 * * * /usr/bin/python3 /home/shopuser/shop/feed_generator.py
0 8 * * 1 python3 ~/seo/weekly_health_check.py
30 8 * * 1 /home/shopuser/scripts/seo_check.sh
15 8 * * 1 python3 /home/shopuser/scripts/meta_generator.py
45 7 * * 1 (cd /home/shopuser/shop && /usr/local/bin/php bin/magento cache:clean full_page)
0 5 * * * python3 /home/shopuser/scripts/price_audit.py
0 9,13,18 * * * python3 /home/shopuser/scripts/stock_sync.py --live
5,35 * * * * /home/shopuser/scripts/purge_feed.sh
* * * * * python3 /home/shopuser/social/render_worker.py
0 4 * * 0 tail -n 20000 /home/shopuser/shop/var/log/cron.log > /tmp/cron_trim.tmp
0 22 * * 0 python3 /home/shopuser/social/generate.py
0 6 * * 5 python3 /home/shopuser/social/generate.py --fallback
0 * * * * python3 /home/shopuser/social/publish.py
0 8 * * 4 python3 /home/shopuser/social/ask_sku.py
*/15 * * * * /home/shopuser/seo/probe.sh >/dev/null 2>&1
0 3 * * * tail -n 20000 /home/shopuser/seo/logs/probe.log > /tmp/probe.log
0 4 * * * [ $(df -P /home/shopuser | awk 'NR==2{print $4}') -lt 2000000 ] && echo low-disk
30 11 * * * /home/shopuser/scripts/stale_check.sh >/dev/null 2>&1
30 7 * * * /bin/bash /home/shopuser/seo/throttle_check.sh >/dev/null 2>&1
0 */2 * * * python3 /home/shopuser/scripts/status_reader.py
0 3 * * * /usr/local/bin/php /home/shopuser/shop/bin/magento indexer:reindex catalogsearch_fulltext
0 6 * * * python3 /home/shopuser/scripts/index_health_check.py
20 * * * * /usr/local/bin/php /home/shopuser/scripts/sync_offers.php --live
"""


@pytest.fixture
def guard_config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"crontab_guard": GUARD}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def no_live_access(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"unexpected live access: {a!r}")
    # A stand-in module on office.jobs only, so the real subprocess stays usable here.
    monkeypatch.setattr(jobs, "subprocess", SimpleNamespace(
        run=refuse, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(jobs, "_fetch", refuse)


def _run(tmp_path, *argv, config=None):
    intray = tmp_path / "intray"
    config = config or tmp_path / "no-config.json"
    code = jobs.main(["--intray", str(intray), "--config", str(config), *argv])
    name = argv[1]
    return code, json.loads((intray / f"{name}.json").read_text(encoding="utf-8")), intray


# ── crontab guard ─────────────────────────────────────────────────────────────

def _check(text, **overrides):
    return jobs.check_crontab(text, overrides.get("baseline", 29), REQUIRED)


def test_crontab_intact():
    status, findings = _check(_crontab())
    assert status == "OK" and findings == ["29 lines (baseline 29)"]


def test_crontab_growth_is_noted_not_alerted():
    status, findings = _check(_crontab(lines=31))
    assert status == "OK" and any("2 line(s) more than the baseline" in f for f in findings)


def test_realistic_crontab_is_ok():
    status, findings = _check(SAMPLE_CRONTAB)
    assert status == "OK", findings


def test_without_a_baseline_only_entries_are_checked():
    status, findings = jobs.check_crontab(_crontab(lines=4), None, REQUIRED)
    assert status == "OK" and "no baseline set" in findings[0]
    status, _ = jobs.check_crontab("", None, None)
    assert status == "ALERT", "the default required entry is the Magento cron"


@pytest.mark.parametrize("text,needle", [
    (_crontab(lines=4), "fewer than the baseline"),
    (_crontab(lines=28), "1 line(s) fewer"),
    (_crontab(drop="Magento cron"), "Magento cron entry"),
    (_crontab(comment="Magento cron"), "Magento cron entry"),
    (_crontab(drop="Probe"), "Probe entry"),
    (_crontab(comment="Publisher"), "Publisher entry"),
    (_crontab(drop="Stock sync"), "Stock sync entry"),
    ("", "fewer than the baseline"),
])
def test_crontab_problems_raise_an_alert(text, needle):
    status, findings = _check(text)
    assert status == "ALERT" and any(needle in f for f in findings), findings


def test_crontab_guard_without_config_says_how_to_set_it_up(tmp_path):
    code, result, _ = _run(tmp_path, "run", "crontab-guard", "--live")
    assert code == 0 and result["status"] == "NEEDS_YOU" and result["live"] is False
    assert "Not configured" in result["summary"] and "config.json" in result["next_step"]


@pytest.mark.parametrize("guard,needle", [
    ({**GUARD, "ssh_target": "-oProxyCommand=evil"}, "user@host"),
    ({**GUARD, "ssh_target": "user@host; rm -rf /"}, "user@host"),
    ({**GUARD, "ssh_port": "2222"}, "ssh_port"),
    ({**GUARD, "ssh_port": 70000}, "ssh_port"),
    ({**GUARD, "ssh_options": ["ProxyCommand=sh -c evil"]}, "ssh_options"),
    ({**GUARD, "ssh_options": ["LocalCommand=evil"]}, "ssh_options"),
    ({**GUARD, "baseline_lines": 0}, "baseline_lines"),
    ({**GUARD, "required_entries": {}}, "required_entries"),
    ({**GUARD, "required_entries": {"x": ""}}, "required_entries"),
])
def test_unsafe_or_invalid_config_is_refused(tmp_path, guard, needle):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"crontab_guard": guard}), encoding="utf-8")
    with pytest.raises(jobs.ConfigError, match=needle):
        jobs.load_guard_config(path)


@pytest.mark.parametrize("content", ["{broken", "[]", '{"other": {}}'])
def test_unreadable_config_is_refused(tmp_path, content):
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(jobs.ConfigError):
        jobs.load_guard_config(path)


def test_crontab_guard_refuses_without_live(tmp_path, guard_config):
    code, result, _ = _run(tmp_path, "run", "crontab-guard", config=guard_config)
    assert code == 0 and result["status"] == "NEEDS_YOU" and result["live"] is False
    assert "--live" in result["next_step"]


def test_crontab_guard_live_runs_only_crontab_list(tmp_path, monkeypatch, guard_config):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=_crontab(), stderr="")
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    code, result, _ = _run(tmp_path, "run", "crontab-guard", "--live", config=guard_config)
    assert code == 0 and result["status"] == "OK" and result["live"] is True
    assert calls == [["ssh", "-p", "2222", "-o", "MACs=hmac-sha2-256", "-o", "BatchMode=yes",
                      "-o", "ConnectTimeout=15", "shopuser@shop.example", "crontab -l"]]


def test_crontab_guard_reports_ssh_failure(tmp_path, monkeypatch, guard_config):
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=255, stdout="", stderr="Permission denied (publickey)"))
    code, result, _ = _run(tmp_path, "run", "crontab-guard", "--live", config=guard_config)
    assert code == 1 and result["status"] == "ERROR" and "Permission denied" in result["summary"]


def test_crontab_guard_alert_exits_nonzero(tmp_path, monkeypatch, guard_config):
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=_crontab(lines=1), stderr=""))
    code, result, _ = _run(tmp_path, "run", "crontab-guard", "--live", config=guard_config)
    assert code == 1 and result["status"] == "ALERT"


# ── social batch ──────────────────────────────────────────────────────────────

def _item(repo, category, item_id, source="LUNNEYS_APPROVED", approval="APPROVED"):
    path = repo / "knowledge" / category / f"{item_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'---\nid: {item_id}\ntitle: "Title of {item_id}"\ncategory: {category}\n'
                    f"source: {source}\napproval_status: {approval}\n---\n\nBody\n", encoding="utf-8")


@pytest.fixture
def social_repo(tmp_path):
    repo = tmp_path / "lunneys-social"
    _item(repo, "kettles", "kettle-descale")
    _item(repo, "kettles", "kettle-capacity")
    _item(repo, "kettles", "kettle-used")
    _item(repo, "hobs", "hob-induction")
    _item(repo, "hobs", "hob-draft", approval="DRAFT")
    _item(repo, "hobs", "hob-external", source="EXTERNAL")
    (repo / "knowledge" / "README.md").write_text("no front matter\n", encoding="utf-8")
    approved = repo / "visuals" / "approved"
    approved.mkdir(parents=True)
    (approved / "kettle-used.json").write_text(json.dumps(
        {"status": "APPROVED", "source_knowledge_id": "kettle-used"}), encoding="utf-8")
    (approved / "broken.json").write_text("{not json", encoding="utf-8")
    return repo


def _tree_digest(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_social_batch_spreads_across_categories_and_skips_used(social_repo):
    picks, approved, used = jobs.pick_social_batch(social_repo, count=2)
    assert approved == 4 and used == 1
    assert {p["category"] for p in picks} == {"kettles", "hobs"}
    ids = {p["id"] for p in picks}
    assert "kettle-used" not in ids and "hob-draft" not in ids and "hob-external" not in ids


def test_social_batch_fills_up_after_one_per_category(social_repo):
    picks, _, _ = jobs.pick_social_batch(social_repo, count=5)
    assert sorted(p["id"] for p in picks) == ["hob-induction", "kettle-capacity", "kettle-descale"]


def test_social_batch_job_gives_runnable_commands_and_changes_nothing(tmp_path, social_repo):
    before = _tree_digest(social_repo)
    code, result, _ = _run(tmp_path, "run", "social-batch", "--social-repo", str(social_repo))
    assert code == 0 and result["status"] == "READY" and len(result["findings"]) == 3
    assert 'python knowledge_run.py "knowledge/hobs/hob-induction.md"' in result["next_step"]
    assert "1 already have an approved visual" in result["summary"]
    assert _tree_digest(social_repo) == before


def test_social_batch_all_posted(tmp_path):
    repo = tmp_path / "repo"
    _item(repo, "kettles", "only")
    (repo / "visuals" / "approved").mkdir(parents=True)
    (repo / "visuals" / "approved" / "only.json").write_text(
        json.dumps({"source_knowledge_id": "only"}), encoding="utf-8")
    _, result, _ = _run(tmp_path, "run", "social-batch", "--social-repo", str(repo))
    assert result["status"] == "OK" and "All 1" in result["summary"]


def test_social_batch_missing_repo(tmp_path):
    code, result, _ = _run(tmp_path, "run", "social-batch", "--social-repo", str(tmp_path / "nope"))
    assert code == 1 and result["status"] == "ERROR"


# ── category images, reviews ──────────────────────────────────────────────────

def test_category_images_without_live_reads_nothing(tmp_path):
    code, result, _ = _run(tmp_path, "run", "category-images")
    assert code == 0 and result["status"] == "NEEDS_YOU" and result["level"] == "CHANGE"
    assert result["live"] is False and "--live" in result["next_step"]


SITEMAP = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
<url><loc>https://loshop.co.uk/</loc><priority>1.0</priority></url>
<url><loc>https://loshop.co.uk/home-appliances.html</loc><priority>0.5</priority></url>
<url><loc>https://loshop.co.uk/home-appliances/washing-machines.html</loc><priority>0.5</priority></url>
<url><loc>https://loshop.co.uk/brands/aeg.html</loc><priority>0.5</priority></url>
<url><loc>https://loshop.co.uk/catalog/category/view/id/42</loc><priority>0.5</priority></url>
<url><loc>https://loshop.co.uk/tv-audio.html</loc><priority>0.5</priority></url>
<url><loc>https://loshop.co.uk/some-kettle.html</loc><priority>1.0</priority><image:image/></url>
<url><loc>https://loshop.co.uk/about-us</loc><priority>0.2</priority></url>
</urlset>"""
LOGO = '<meta property="og:image" content="https://loshop.co.uk/media/logo/stores/1/logo.png"/>'
OWN = '<meta content="https://loshop.co.uk/media/catalog/category/wm.jpg" property="og:image">'
PAGES = {
    "https://loshop.co.uk/home-appliances.html": (200, LOGO),
    "https://loshop.co.uk/home-appliances/washing-machines.html": (200, OWN),
    "https://loshop.co.uk/brands/aeg.html": (200, LOGO),
    "https://loshop.co.uk/catalog/category/view/id/42": (404, ""),
    "https://loshop.co.uk/tv-audio.html": (200, "<html>no og tags</html>"),
}


def test_category_urls_are_the_priority_half_entries():
    assert jobs.category_urls(SITEMAP) == list(PAGES)


def test_og_image_reads_either_attribute_order():
    assert jobs.og_image(LOGO).endswith("logo.png")
    assert jobs.og_image(OWN).endswith("wm.jpg")
    assert jobs.og_image("<html></html>") is None


@pytest.mark.parametrize("url,group", [
    ("https://loshop.co.uk/home-appliances.html", "home-appliances"),
    ("https://loshop.co.uk/home-appliances/washing-machines.html", "home-appliances"),
    ("https://loshop.co.uk/brands/aeg.html", "brands"),
    ("https://loshop.co.uk/catalog/category/view/id/42", "no friendly URL"),
])
def test_category_group(url, group):
    assert jobs.category_group(url) == group


def test_ccp3_audit_reports_logo_fallbacks(tmp_path, monkeypatch):
    fetched = []

    def fake_fetch(url, limit=200_000):
        fetched.append(url)
        if url.endswith("sitemap.xml"):
            return 200, SITEMAP
        if url.endswith("tv-audio.html"):
            raise OSError("timed out")
        return PAGES[url]
    monkeypatch.setattr(jobs, "_fetch", fake_fetch)
    code, result, intray = _run(tmp_path, "run", "category-images", "--live", "--delay", "0")
    assert code == 0 and result["status"] == "NEEDS_YOU" and result["live"] is True
    assert result["summary"].startswith("Phase 1 done: 4 of 5 category pages")
    assert fetched[0] == "https://loshop.co.uk/sitemap.xml" and len(fetched) == 6
    report = (intray / "ccp3-audit.md").read_text(encoding="utf-8")
    assert "| home-appliances | 2 | 1 |" in report
    assert "brands/aeg.html (logo fallback)" in report
    assert "view/id/42 (HTTP 404)" in report and "tv-audio.html (error: timed out)" in report
    csv_text = (intray / "ccp3-audit.csv").read_text(encoding="utf-8")
    assert csv_text.startswith("group,url,verdict,og_image,status")
    assert "washing-machines.html,has image" in csv_text


def test_ccp3_audit_all_good(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=200_000: (
        (200, SITEMAP) if url.endswith("sitemap.xml") else (200, OWN)))
    code, result, _ = _run(tmp_path, "run", "category-images", "--live", "--delay", "0")
    assert code == 0 and result["status"] == "OK" and "All 5" in result["summary"]


def test_ccp3_audit_sitemap_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=200_000: (503, ""))
    code, result, _ = _run(tmp_path, "run", "category-images", "--live", "--delay", "0")
    assert code == 1 and result["status"] == "ERROR"


def test_audit_pauses_between_pages(monkeypatch):
    sleeps = []
    monkeypatch.setattr(jobs.time, "sleep", sleeps.append)
    jobs.audit_category_images(["a", "b", "c"], lambda u: (200, OWN), delay=0.5)
    assert sleeps == [0.5, 0.5]


def test_reviews_without_input_needs_a_connection(tmp_path):
    _, result, _ = _run(tmp_path, "run", "reviews-replies")
    assert result["status"] == "NEEDS_CONNECTION" and result["findings"] == jobs.REPLY_RULES


def test_reviews_pack_sends_low_ratings_to_the_boss(tmp_path):
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([
        {"name": "Ann", "rating": 5, "text": "Great service in Banbridge", "product": "Kettle"},
        {"name": "Bob", "rating": "2", "text": "Late delivery"},
        {"name": "Cy", "rating": "n/a", "text": "?"},
    ]), encoding="utf-8")
    code, result, intray = _run(tmp_path, "run", "reviews-replies", "--input", str(reviews))
    assert code == 0 and result["status"] == "NEEDS_YOU"
    assert result["summary"].startswith("1 replies to draft; 2 low-rated")
    brief = (intray / "reviews-reply-brief.md").read_text(encoding="utf-8")
    assert "### Ann · 5 stars · Kettle" in brief
    head, boss = brief.split("## For the boss first")
    assert "Bob" in boss and "Cy" in boss and "Bob" not in head


@pytest.mark.parametrize("content", ['{"name": "x"}', "[1, 2]", "not json"])
def test_reviews_bad_input_is_an_error(tmp_path, content):
    path = tmp_path / "r.json"
    path.write_text(content, encoding="utf-8")
    code, result, _ = _run(tmp_path, "run", "reviews-replies", "--input", str(path))
    assert code == 1 and result["status"] == "ERROR"


# ── robots / morning briefing ─────────────────────────────────────────────────

def test_robots_intact():
    assert jobs.check_robots(ROBOTS_OK) == []


def test_robots_googlebot_rule_is_matched_as_a_whole_line():
    text = ROBOTS_OK.replace("User-agent: Googlebot\nDisallow:\n\n", "")
    assert "Googlebot-image" in text
    assert jobs.check_robots(text) == ["explicit Googlebot rule missing"]


def test_robots_query_string_disallow_flagged():
    assert any("layered navigation" in p for p in jobs.check_robots(ROBOTS_OK + "Disallow: /*?\n"))


def test_briefing_collects_results_without_going_online(tmp_path, monkeypatch, guard_config):
    monkeypatch.setattr(jobs.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=_crontab(lines=2), stderr=""))
    _run(tmp_path, "run", "crontab-guard", "--live", config=guard_config)
    code, result, intray = _run(tmp_path, "run", "morning-briefing",
                                "--relay-db", str(tmp_path / "r.db"), "--a2a-db", str(tmp_path / "a.db"))
    assert code == 1 and result["status"] == "ALERT", "a crontab ALERT must surface in the briefing"
    text = "\n".join(result["findings"])
    assert "Crontab guard: ALERT:" in text and "This week's social batch: not run yet" in text
    assert "Website checks skipped" in text and "Base.com" in text
    assert list(intray.glob("morning-briefing-*.md"))
    assert not (tmp_path / "r.db").exists(), "the briefing must not create databases"


@pytest.mark.parametrize("robots,sitemap,status", [
    (ROBOTS_OK, '<?xml version="1.0"?><urlset>', "OK"),
    (ROBOTS_OK.replace("Sitemap: https://loshop.co.uk/sitemap.xml", ""), "<urlset>", "ALERT"),
    (ROBOTS_OK, "<html>error</html>", "ALERT"),
])
def test_briefing_live_website_checks(tmp_path, monkeypatch, robots, sitemap, status):
    urls = []

    def fake_fetch(url):
        urls.append(url)
        return 200, robots if url.endswith("robots.txt") else sitemap
    monkeypatch.setattr(jobs, "_fetch", fake_fetch)
    _, result, _ = _run(tmp_path, "run", "morning-briefing", "--live",
                        "--relay-db", str(tmp_path / "r.db"), "--a2a-db", str(tmp_path / "a.db"))
    assert result["status"] == status
    assert urls == ["https://loshop.co.uk/robots.txt", "https://loshop.co.uk/sitemap.xml"]


# ── plumbing and dashboard ────────────────────────────────────────────────────

def test_list_shows_all_five_jobs(capsys):
    assert jobs.main(["list"]) == 0
    out = capsys.readouterr().out
    for name in ("crontab-guard", "morning-briefing", "social-batch", "category-images",
                 "reviews-replies"):
        assert name in out


def test_dashboard_shows_the_intray_escaped(tmp_path):
    intray = tmp_path / "intray"
    jobs.save_result(intray, jobs.JobResult(
        job="social-batch", title="This week's social batch", desk="d", level="DRAFT",
        status="READY", summary='<script>alert("x")</script>', findings=["<b>kettles</b>"],
        next_step="cd x && run"))
    (intray / "crontab-guard.json").write_text("{broken", encoding="utf-8")
    snap = snapshot(tmp_path / "r.db", tmp_path / "a.db", intray)
    assert [j["job"] for j in snap.intray] == ["social-batch"]
    page = render(snap, tmp_path / "r.db", tmp_path / "a.db")
    assert "In-tray: 1" in page and "<script>" not in page
    assert "&lt;b&gt;kettles&lt;/b&gt;" in page and "cd x &amp;&amp; run" in page


def test_cli_module_entry_point(tmp_path):
    r = subprocess.run([sys.executable, "-m", "office.jobs", "--intray", str(tmp_path / "t"),
                        "run", "category-images"],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "[NEEDS_YOU] Category images backfill" in r.stdout
    assert (tmp_path / "t" / "category-images.json").is_file()


# ── review findings (malformed input, consoles, line endings) ─────────────────

def test_robots_with_windows_line_endings_and_lowercase_disallow():
    crlf = ROBOTS_OK.replace("\n", "\r\n")
    assert jobs.check_robots(crlf) == []
    assert any("layered navigation" in p for p in jobs.check_robots(crlf + "disallow: /*?\r\n"))


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', '{"title": "no status"}'])
def test_briefing_survives_malformed_intray_files(tmp_path, content):
    intray = tmp_path / "intray"
    intray.mkdir()
    (intray / "crontab-guard.json").write_text(content, encoding="utf-8")
    code, result, _ = _run(tmp_path, "run", "morning-briefing",
                           "--relay-db", str(tmp_path / "r.db"), "--a2a-db", str(tmp_path / "a.db"))
    assert result["job"] == "morning-briefing" and list(intray.glob("morning-briefing-*.md"))


def test_briefing_shows_the_age_of_each_result(tmp_path):
    _run(tmp_path, "run", "category-images")
    _, result, _ = _run(tmp_path, "run", "morning-briefing",
                        "--relay-db", str(tmp_path / "r.db"), "--a2a-db", str(tmp_path / "a.db"))
    line = next(f for f in result["findings"] if f.startswith("Category images"))
    assert "(as of " in line and "UTC" not in line


def test_dashboard_survives_malformed_intray_files(tmp_path):
    intray = tmp_path / "intray"
    intray.mkdir()
    (intray / "social-batch.json").write_text(json.dumps(
        {"status": ["ALERT"], "findings": 7, "title": {"x": 1}, "generated_at": 5}), encoding="utf-8")
    (intray / "crontab-guard.json").write_text("[1]", encoding="utf-8")
    snap = snapshot(tmp_path / "r.db", tmp_path / "a.db", intray)
    assert len(snap.intray) == 1 and snap.intray[0]["findings"] == []
    assert "In-tray: 1" in render(snap, tmp_path / "r.db", tmp_path / "a.db")


def test_social_batch_tolerates_odd_files(tmp_path, social_repo):
    approved = social_repo / "visuals" / "approved"
    (approved / "list.json").write_text("[1]", encoding="utf-8")
    (approved / "listid.json").write_text('{"source_knowledge_id": ["x"]}', encoding="utf-8")
    (social_repo / "knowledge" / "folder.md").mkdir()
    bom = social_repo / "knowledge" / "toasters" / "bom.md"
    bom.parent.mkdir()
    bom.write_text("﻿---\nid: bom-item\ncategory: toasters\nsource: 'LUNNEYS_APPROVED'\n"
                   "approval_status: 'APPROVED'\n---\n", encoding="utf-8")
    picks, approved_total, _ = jobs.pick_social_batch(social_repo, count=10)
    assert approved_total == 5 and "bom-item" in {p["id"] for p in picks}
    assert jobs.pick_social_batch(social_repo, count=0)[0] == []


def test_count_must_be_positive(tmp_path):
    with pytest.raises(SystemExit):
        jobs.main(["--intray", str(tmp_path), "run", "social-batch", "--count", "0"])


def test_review_text_cannot_add_headings(tmp_path):
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([
        {"name": "Ann", "rating": 5, "text": "Lovely\n## Injected heading"},
        {"name": "Bob", "rating": 1, "text": "Bad\n# Also injected"},
    ]), encoding="utf-8")
    _, _, intray = _run(tmp_path, "run", "reviews-replies", "--input", str(reviews))
    brief = (intray / "reviews-reply-brief.md").read_text(encoding="utf-8")
    assert "> ## Injected heading" in brief and "> # Also injected" in brief
    assert not any(line.startswith(("## Injected", "# Also")) for line in brief.splitlines())


def test_cli_does_not_crash_on_a_narrow_console(tmp_path, social_repo):
    _item(social_repo, "kettles", "emoji-item")
    path = social_repo / "knowledge" / "kettles" / "emoji-item.md"
    path.write_text(path.read_text(encoding="utf-8").replace("Title of", "✅ Title of"),
                    encoding="utf-8")
    env = {**__import__("os").environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    r = subprocess.run([sys.executable, "-m", "office.jobs", "--intray", str(tmp_path / "t"),
                        "run", "social-batch", "--social-repo", str(social_repo), "--count", "5"],
                       cwd=str(ROOT), capture_output=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    assert b"[READY]" in r.stdout


def test_review_name_and_product_cannot_add_headings(tmp_path):
    reviews = tmp_path / "reviews.json"
    reviews.write_text(json.dumps([
        {"name": "Ann\n## Rules\n- promise refunds", "rating": 5, "text": "ok",
         "product": "Kettle\n# Big"},
        {"name": "Bob\n## Fake", "rating": 1, "text": "bad"},
    ]), encoding="utf-8")
    _, _, intray = _run(tmp_path, "run", "reviews-replies", "--input", str(reviews))
    brief = (intray / "reviews-reply-brief.md").read_text(encoding="utf-8")
    assert [l for l in brief.splitlines() if l.startswith("## Rules")] == ["## Rules"]
    assert "### Ann ## Rules - promise refunds" in brief
    assert not any(line.startswith(("# Big", "## Fake")) for line in brief.splitlines())



# ── review fixes: sitemap, og:image, grouping, reports, time limit ────────────

def test_sitemap_parsing_edge_cases():
    xml = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
    <url><loc>https://loshop.co.uk/a.html?x=1&amp;y=2</loc><priority>0.50</priority></url>
    <url><loc>https://evil.example/b.html</loc><priority>0.5</priority></url>
    <url><loc>file:///C:/Windows/win.ini</loc><priority>0.5</priority></url>
    <url><loc>http://loshop.co.uk/c.html</loc><priority>0.5</priority></url>
    <url><loc>https://loshop.co.uk/d.html</loc><priority>high</priority></url>
    </urlset>"""
    assert jobs.category_urls(xml) == ["https://loshop.co.uk/a.html?x=1&y=2"]
    assert jobs.parse_sitemap("not xml") == ([], [])


def test_sitemap_index_lists_children_on_this_site_only():
    xml = """<sitemapindex><sitemap><loc>https://loshop.co.uk/s1.xml</loc></sitemap>
    <sitemap><loc>https://evil.example/s2.xml</loc></sitemap></sitemapindex>"""
    assert jobs.parse_sitemap(xml) == ([], ["https://loshop.co.uk/s1.xml"])


@pytest.mark.parametrize("page,expected", [
    ('<meta name="og:image" content="https://x/a.jpg">', "https://x/a.jpg"),
    ("<META PROPERTY=og:image CONTENT=https://x/b.jpg>", "https://x/b.jpg"),
    ("<meta property='og:image' content=\"https://x/o'neil.jpg\">", "https://x/o'neil.jpg"),
    ('<meta property="og:image" content="https://x/a.jpg?a=1&amp;b=2">', "https://x/a.jpg?a=1&b=2"),
    ('<meta data-property="og:image" content="https://x/no.jpg">', None),
    ('<meta property="og:image:secure_url" content="https://x/no.jpg">', None),
])
def test_og_image_parsing(page, expected):
    assert jobs.og_image(page) == expected


def test_relative_og_image_counts_as_missing():
    assert jobs.classify_page(200, '<meta property="og:image" content="/media/x.jpg">')[0] == \
        "relative og:image"


@pytest.mark.parametrize("url,group", [
    ("https://loshop.co.uk", "(home)"),
    ("HTTPS://loshop.co.uk/tv.html#top", "tv"),
    ("https://loshop.co.uk/floorcare.html?p=2", "floorcare"),
])
def test_category_group_edge_cases(url, group):
    assert jobs.category_group(url) == group


def test_reports_resist_injection(tmp_path, monkeypatch):
    sitemap = """<urlset><url><loc>https://loshop.co.uk/a|b.html</loc><priority>0.5</priority></url>
    <url><loc>https://loshop.co.uk/c.html</loc><priority>0.5</priority></url></urlset>"""
    pages = {"https://loshop.co.uk/a|b.html": (200, LOGO),
             "https://loshop.co.uk/c.html": (200, '<meta property="og:image" content="=cmd|x">')}
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=200_000: (
        (200, sitemap) if url.endswith("sitemap.xml") else pages[url]))
    _, _, intray = _run(tmp_path, "run", "category-images", "--live", "--delay", "0")
    report = (intray / "ccp3-audit.md").read_text(encoding="utf-8")
    assert "a\\|b.html" in report
    csv_text = (intray / "ccp3-audit.csv").read_text(encoding="utf-8")
    assert "'=cmd|x" in csv_text and ",=cmd" not in csv_text


def test_audit_stops_at_the_deadline_and_keeps_partial_results(tmp_path, monkeypatch):
    clock = iter([0, 0, 100, 200, 300, 400, 500])
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))
    sitemap = "<urlset>" + "".join(
        f"<url><loc>https://loshop.co.uk/p{i}.html</loc><priority>0.5</priority></url>"
        for i in range(5)) + "</urlset>"
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=200_000: (
        (200, sitemap) if url.endswith("sitemap.xml") else (200, LOGO)))
    _, result, intray = _run(tmp_path, "run", "category-images", "--live", "--delay", "0",
                             "--max-minutes", "1")
    assert result["findings"][0].startswith("Report: ")
    assert any(f.startswith("Stopped early at the 1-minute limit: 1 of 5") for f in result["findings"])
    assert "Stopped early" in (intray / "ccp3-audit.md").read_text(encoding="utf-8")
    assert len((intray / "ccp3-audit.csv").read_text(encoding="utf-8").splitlines()) == 2


def test_fetch_turns_http_errors_into_statuses(monkeypatch):
    import urllib.error

    def boom(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
    monkeypatch.setattr(jobs.urllib.request, "urlopen", boom)
    assert REAL_FETCH("https://loshop.co.uk/x.html") == (404, "")


BOT_PAGE = "<!DOCTYPE html><html><head><title>One moment, please...</title></head></html>"


def test_bot_check_page_is_retried_after_a_pause(monkeypatch):
    sleeps, served = [], {"n": 0}
    monkeypatch.setattr(jobs.time, "sleep", sleeps.append)

    def fetch(url):
        served["n"] += 1
        return (200, BOT_PAGE) if served["n"] == 1 else (200, OWN)
    rows = jobs.audit_category_images(["https://loshop.co.uk/a.html"], fetch, delay=0, bot_wait=20)
    assert rows[0]["verdict"] == "has image" and sleeps == [20]


def test_persistent_bot_check_is_reported_as_such(monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    rows = jobs.audit_category_images(["https://loshop.co.uk/a.html"],
                                      lambda u: (200, BOT_PAGE), delay=0)
    assert rows[0]["verdict"] == "bot check"


@pytest.mark.parametrize("value,expected", [
    ("2026-10-16T13:51:00+00:00", "2026-10-16 14:51"),   # BST: UTC+1
    ("2026-12-01T13:51:00+00:00", "2026-12-01 13:51"),   # GMT
    ("2026-10-16T13:51:00", "2026-10-16 14:51"),         # naive means UTC
    ("", ""),
    ("not a time", "not a time"),
])
def test_uk_time(value, expected):
    if jobs.UK_TZ is None:
        pytest.skip("no time-zone data on this machine")
    assert jobs.uk_time(value) == expected


@pytest.mark.parametrize("content,expected", [
    ('{"retired_jobs": ["category-images", "nope", 3]}', {"category-images"}),
    ('{"retired_jobs": "category-images"}', set()),
    ("[]", set()),
    ("{broken", set()),
])
def test_retired_jobs_reads_only_known_names(tmp_path, content, expected):
    path = tmp_path / "c.json"
    path.write_text(content, encoding="utf-8")
    assert jobs.retired_jobs(path) == expected
    assert jobs.retired_jobs(tmp_path / "missing.json") == frozenset()


def test_briefing_and_list_skip_retired_jobs(tmp_path, capsys):
    config = tmp_path / "c.json"
    config.write_text('{"retired_jobs": ["category-images"]}', encoding="utf-8")
    _, result, _ = _run(tmp_path, "run", "morning-briefing", "--relay-db", str(tmp_path / "r.db"),
                        "--a2a-db", str(tmp_path / "a.db"), config=config)
    assert not any("Category images" in f for f in result["findings"])
    assert any("Crontab guard" in f for f in result["findings"])
    capsys.readouterr()
    jobs.main(["--config", str(config), "list"])
    out = capsys.readouterr().out
    assert "category-images    (retired)" in out and "crontab-guard" in out
