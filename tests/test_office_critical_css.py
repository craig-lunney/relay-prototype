"""
Critical CSS drift check (CCP 11/12).

No test reaches the live site: page and stylesheet fetches are replaced with
fixture strings.
"""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from office import jobs

HEADER = ".lunneys-topbar{background:#0c1a4a;color:#b8c4de}.breadcrumbs .item+.item:before{color:#fff}"
USP = ".lun-usp-strip{display:flex}@media (max-width:767px){.lun-usp__icon{width:24px}}"
MERGED = ('@charset "UTF-8";/* banner */@font-face{font-family:x;src:url(x.woff2)}body{margin:0}'
          "h1,.x{font-size:2em}h1 a{color:red}.page-title-wrapper .page-title{margin:0}"
          "@media (min-width:768px){.lun-offers{display:grid}.foo{a:b}}@media print{.bar{x:y}}"
          "@keyframes k{from{opacity:0}to{opacity:1}}.breadcrumbs{margin-bottom:20px}"
          "@supports (display:grid){.lun-cat-tile{display:grid}}")
RULES = ("h1,.x{font-size:2em}.page-title-wrapper .page-title{margin:0}"
         "@media (min-width:768px){.lun-offers{display:grid}}.breadcrumbs{margin-bottom:20px}"
         "@supports (display:grid){.lun-cat-tile{display:grid}}")
V = "version1792166283"
STATIC = f"https://loshop.co.uk/static/{V}"
HOME_MERGED = f"{STATIC}/_cache/merged/991dc5edcc564045410a5c5e33f9e161.min.css"
CAT_MERGED = f"{STATIC}/_cache/merged/755f6b7f7c128ce026714b3a53707d33.min.css"
PRODUCT_MERGED = f"{STATIC}/_cache/merged/bfc8e77b30fa508ffc791ab98b05a09d.min.css"


def _critical(ccp12=True, header=HEADER, usp=USP, rules=RULES):
    text = ".old-14-may{color:red}\n/* CCP 11: header + USP strip, copied on 2026-10-17. */\n" + header + usp
    if ccp12:
        text += "\n/* CCP 12: offers grid, base headings, copied by extract.py. */\n" + rules
        text += "\n/* CCP 12: keep filter groups closed. */\n.filter:has(.x){display:none}"
    return text


def _page(critical, merged):
    style = "" if critical is None else (
        f'<style id="lunneys-critical-css" data-page="x">\n{critical}\n</style>')
    return (f"<html><head>{style}"
            f'<link rel="stylesheet" media="print" href="{merged}" onload="this.media=\'all\'">'
            f'<link rel="icon" href="https://loshop.co.uk/static/{V}/frontend/Lunneys/blank/en_GB/x.ico">'
            f'<noscript><link rel="stylesheet" href="{merged}"></noscript></head><body></body></html>')


def _site(**override):
    files = {
        "https://loshop.co.uk/": _page(_critical(), HOME_MERGED),
        "https://loshop.co.uk/home-appliances/laundry/washing-machines.html": _page(_critical(), CAT_MERGED),
        "https://loshop.co.uk/samsung-rs70f66kcfeu-91cm-side-by-side-american-fridge-freezer-"
        "stainless-steel-black.html": _page(_critical(ccp12=False), PRODUCT_MERGED),
        f"{STATIC}/frontend/Lunneys/blank/en_GB/Lunneys_Seo/css/lunneys-header.min.css": HEADER + "\n",
        f"{STATIC}/frontend/Lunneys/blank/en_GB/Lunneys_Seo/css/usp-strip.min.css": USP,
        HOME_MERGED: MERGED,
        CAT_MERGED: MERGED,
    }
    files.update(override)
    return files


@pytest.fixture(autouse=True)
def no_live_access(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError(f"unexpected live access: {a!r}")
    monkeypatch.setattr(jobs, "subprocess", SimpleNamespace(
        run=refuse, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(jobs, "_fetch", refuse)
    monkeypatch.setattr(jobs, "_fetch_bytes", refuse)


def _serve(monkeypatch, files):
    asked = []

    def fetch(url, limit=0):
        asked.append(url)
        bare = url.split("?", 1)[0]
        return (200, files[bare]) if files.get(bare) is not None else (404, "")
    monkeypatch.setattr(jobs, "_fetch", fetch)
    return asked


def _run(tmp_path, *extra):
    intray = tmp_path / "intray"
    code = jobs.main(["--intray", str(intray), "--config", str(tmp_path / "none.json"),
                      "run", "critical-css-drift", "--delay", "0", *extra])
    return code, json.loads((intray / "critical-css-drift.json").read_text(encoding="utf-8"))


# ── the pieces ────────────────────────────────────────────────────────────────

def test_extract_matches_ccp12_extract_script():
    rules, count = jobs.extract_css_rules(MERGED)
    assert rules == RULES and count == 5


def test_extract_keeps_order_and_skips_comments_inside_selectors():
    css = "/* h1{x:y} */.a{b:c}h2{d:e}@media (x){h3,.b{f:g}}h4{h:i}.lun-offer__price{j:k}"
    assert jobs.extract_css_rules(css) == ("h2{d:e}@media (x){h3,.b{f:g}}.lun-offer__price{j:k}", 3)


def test_page_css_links_and_critical_block():
    page = _page(_critical(), HOME_MERGED)
    assert jobs.page_css_links(page) == (V, HOME_MERGED)
    assert jobs.critical_css(page).startswith("\n.old-14-may")
    assert jobs.critical_css("<html><style>.a{}</style></html>") is None
    off_site = '<link href="https://cdn.example/static/version1/_cache/merged/ab.min.css">'
    assert jobs.page_css_links(off_site) == (None, None)


def test_check_critical_block_looks_only_after_the_mark():
    critical = HEADER + "\n/* CCP 11: header + USP strip */\n" + USP
    assert jobs.check_critical_block(critical, jobs.CCP11_MARK, {"h": HEADER, "u": USP}) == ["h"]
    assert jobs.check_critical_block(".a{}", jobs.CCP11_MARK, {"h": HEADER, "u": USP}) == ["h", "u"]


# ── the job ───────────────────────────────────────────────────────────────────

def test_needs_live(tmp_path):
    code, result = _run(tmp_path)
    assert code == 0 and result["status"] == "NEEDS_YOU" and "--live" in result["next_step"]


def test_all_blocks_match(tmp_path, monkeypatch):
    asked = _serve(monkeypatch, _site())
    code, result = _run(tmp_path, "--live")
    assert code == 0 and result["status"] == "OK", result
    assert result["live"] is True
    assert sum("CCP 11 block matches" in f for f in result["findings"]) == 3
    assert sum("CCP 12 block matches (5 rules" in f for f in result["findings"]) == 2
    pages = [u for u in asked if not u.endswith(".css")]
    assert len(pages) == 3 and all("?office=" in u for u in pages)
    assert PRODUCT_MERGED not in asked                    # product pages have no CCP 12 block
    assert len(asked) == len(set(asked))                  # each stylesheet is fetched once


def test_header_change_alerts_on_every_page(tmp_path, monkeypatch):
    _serve(monkeypatch, _site(**{
        f"{STATIC}/frontend/Lunneys/blank/en_GB/Lunneys_Seo/css/lunneys-header.min.css":
            HEADER.replace("#0c1a4a", "#0c1a4b")}))
    code, result = _run(tmp_path, "--live")
    assert code == 1 and result["status"] == "ALERT"
    for name in ("home CCP 11", "category CCP 11", "product CCP 11"):
        assert name in result["summary"]
    assert "CCP 12" not in result["summary"]
    assert any("lunneys-header.min.css no longer appears verbatim" in f for f in result["findings"])
    assert result["next_step"] == ("refresh the CCP 11/12 block in app/code/Lunneys/CriticalCss/"
                                   "critical/*.css (see ccps_ready.md CCP 11/12)")


def test_merged_css_change_alerts_on_that_page_only(tmp_path, monkeypatch):
    _serve(monkeypatch, _site(**{CAT_MERGED: MERGED.replace("margin-bottom:20px", "margin-bottom:24px")}))
    code, result = _run(tmp_path, "--live")
    assert code == 1 and result["status"] == "ALERT"
    assert "category CCP 12" in result["summary"] and "home CCP 12" not in result["summary"]
    assert "CCP 11" not in result["summary"]
    assert any(f.startswith("category: CCP 12 block drifted: the 5 matching rules") for f in result["findings"])


def test_new_matching_rule_counts_as_drift(tmp_path, monkeypatch):
    _serve(monkeypatch, _site(**{HOME_MERGED: MERGED + ".lun-offer__badge{top:0}"}))
    _, result = _run(tmp_path, "--live")
    assert result["status"] == "ALERT" and "home CCP 12" in result["summary"]


@pytest.mark.parametrize("override,needle", [
    ({"https://loshop.co.uk/": _page(_critical(ccp12=False), HOME_MERGED)}, "home CCP 12"),
    ({"https://loshop.co.uk/": _page(_critical().replace("CCP 11", "CCP 1l"), HOME_MERGED)}, "home CCP 11"),
    ({"https://loshop.co.uk/samsung-rs70f66kcfeu-91cm-side-by-side-american-fridge-freezer-"
      "stainless-steel-black.html": _page(None, PRODUCT_MERGED)}, "product (no critical CSS)"),
])
def test_missing_block_alerts(tmp_path, monkeypatch, override, needle):
    _serve(monkeypatch, _site(**override))
    _, result = _run(tmp_path, "--live")
    assert result["status"] == "ALERT" and needle in result["summary"], result


def test_unreadable_stylesheet_is_an_error_not_ok(tmp_path, monkeypatch):
    _serve(monkeypatch, _site(**{HOME_MERGED: None}))
    code, result = _run(tmp_path, "--live")
    assert code == 1 and result["status"] == "ERROR" and "HTTP 404" in result["summary"]


def test_page_without_merged_link_is_an_error(tmp_path, monkeypatch):
    _serve(monkeypatch, _site(**{"https://loshop.co.uk/": _page(_critical(), "https://loshop.co.uk/x.css")}))
    _, result = _run(tmp_path, "--live")
    assert result["status"] == "ERROR" and "no _cache/merged" in result["summary"]


def test_bot_check_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs.time, "sleep", lambda s: None)
    _serve(monkeypatch, _site(**{"https://loshop.co.uk/": "<title>One moment, please...</title>"}))
    _, result = _run(tmp_path, "--live")
    assert result["status"] == "ERROR" and "bot check" in result["summary"]
