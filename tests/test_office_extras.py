"""
Share-preview checker, backup check and Windows alerts.

No test reaches the live site, the server or the Windows notification system:
fetches, SSH and PowerShell are all replaced.
"""
from __future__ import annotations

import base64
import re
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



# ── product copy check ────────────────────────────────────────────────────────

def _p(**kw):
    base = {"sku": "1", "name": "Bosch Serie 4 Washing Machine", "url_key": "bosch-wm",
            "meta_title": "Bosch Serie 4", "visibility": 4,
            "meta_description": "Bosch Serie 4 washing machine with quick wash.",
            "description": "<p>A freestanding washing machine.</p>", "short_description": ""}
    return {**base, **kw}


def test_clean_product_has_no_issues():
    assert jobs.check_product_copy(_p()) == []


@pytest.mark.parametrize("product,issue", [
    (_p(name="Dyson BIG BALL Dyson Big Ball - CY26 653668-01"), "name repeats words"),
    (_p(name="Dyson Big Ball CY26", meta_description="Dyson Big Ball CY26 upright vacuum cleaner.",
        description="The Dyson Big Ball cylinder vacuum features cyclones."), "vacuum type contradiction"),
    (_p(meta_description="A cordless vacuum.", description="Powerful corded vacuum."), "power contradiction"),
    (_p(meta_description="Integrated washing machine.", description="A freestanding washer."),
     "installation contradiction"),
    (_p(meta_description="Great washer. Free delivery on orders.", is_euronics="1"), "promise to check"),
    (_p(meta_description="Great washer. Next day delivery."), "promise to check"),
    (_p(meta_description=""), "no meta description"),
    (_p(meta_description="x" * 170), "meta description long"),
    (_p(meta_title="Bosch Serie 4 | Lunneys Electrical"), "meta title repeats Lunneys"),
])
def test_copy_issues(product, issue):
    assert issue in [i for i, _ in jobs.check_product_copy(product)]


def test_no_false_alarms():
    # model numbers repeating digits, a promise the product page itself makes, "freestanding"
    # matched as a whole word only, stick vacuums counted as cordless
    assert jobs.repeated_phrase("LG 55 inch 55UR78 TV 55 inch") == "55 inch"
    assert jobs.repeated_phrase("Samsung 1 2 3 1 2 3") is None
    assert jobs.check_product_copy(_p(meta_description="Free delivery included.",
                                      description="Free delivery included on this item.")) == []
    assert jobs.check_product_copy(_p(meta_description="Cordless stick vacuum.",
                                      description="A stick vacuum that is cordless.")) == []


def test_fetch_products_pages_until_done():
    pages = {1: {"items": [{"sku": "a", "name": "A", "custom_attributes": [
                 {"attribute_code": "meta_description", "value": "m"}]}], "total_count": 2},
             2: {"items": [{"sku": "b", "name": "B", "custom_attributes": []}], "total_count": 2}}
    seen = []

    def get(path):
        seen.append(path)
        assert path.startswith("/rest/all/V1/products?") and "status" in path
        return pages[int(path.split("currentPage%5D=")[1].split("&")[0])]
    products = jobs.fetch_products({"base": "x", "token": "t"}, page_size=1, get=get)
    assert [p["sku"] for p in products] == ["a", "b"] and products[0]["meta_description"] == "m"
    assert len(seen) == 2


def test_product_copy_job_writes_a_safe_csv(tmp_path, monkeypatch):
    ini = tmp_path / "scripts.ini"
    ini.write_text("[magento]\nbase_url = https://loshop.co.uk\ntoken = SECRET-TOKEN\n", encoding="utf-8")
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"magento_api": {"config_ini": str(ini)}}), encoding="utf-8")
    monkeypatch.setattr(jobs, "fetch_products", lambda api: [
        _p(sku="1", name="=HYPERLINK(1) Dyson Dyson x x", meta_description="Free delivery.",
           is_euronics="1"),
        _p(sku="2"), _p(sku="3", visibility=1, name="Hidden Hidden Hidden Hidden")])
    code, result, intray = _run(tmp_path, "run", "product-copy", "--live", config=config)
    assert code == 0 and result["status"] == "NEEDS_YOU"
    assert result["summary"] == "1 of 2 products have copy to check."
    text = (intray / "product-copy.csv").read_text(encoding="utf-8")
    assert "'=HYPERLINK" in text and "Hidden" not in text and "SECRET-TOKEN" not in text
    assert "SECRET-TOKEN" not in json.dumps(result)


@pytest.mark.parametrize("ini_text,needle", [
    ("[magento]\nbase_url = https://evil.example\ntoken = t\n", "base_url"),
    ("[magento]\nbase_url = https://loshop.co.uk\ntoken =\n", "token"),
    ("[other]\nx = 1\n", "[magento]"),
])
def test_api_config_is_checked(tmp_path, ini_text, needle):
    ini = tmp_path / "s.ini"
    ini.write_text(ini_text, encoding="utf-8")
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"magento_api": {"config_ini": str(ini)}}), encoding="utf-8")
    with pytest.raises(jobs.ConfigError, match=re.escape(needle)):
        jobs.load_api_config(config)


def test_free_delivery_is_fine_except_on_euronics_items():
    meta = "Bosch washer. Free delivery available."
    assert jobs.check_product_copy(_p(meta_description=meta)) == []
    assert jobs.check_product_copy(_p(meta_description=meta, is_euronics="0")) == []
    assert [i for i, _ in jobs.check_product_copy(_p(meta_description=meta, is_euronics="1"))] ==         ["promise to check"]


# ── AI shopping readiness ─────────────────────────────────────────────────────

LIVE_ROBOTS = """User-agent: Amazonbot
Allow: /

User-agent: Applebot-Extended
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: ClaudeBot
Allow: /

User-agent: GPTBot
Allow: /

User-agent: Googlebot
Disallow:

User-agent: *
Disallow: /admin/
Disallow: /checkout/

Sitemap: https://loshop.co.uk/sitemap.xml
"""
LLMS = "# Lunneys Electrical\n\n> Family-run appliance retailer since 1967.\n\n" + "About us. " * 30


def _ai(**kw):
    base = {"sku": "BMN3WT3841S", "name": "Beko BMN3WT3841S 8kg Washing Machine - Silver",
            "type_id": "simple", "price": 329, "url_key": "beko-bmn3wt3841s", "visibility": 4,
            "meta_title": "Beko BMN3WT3841S", "meta_description": "Beko 8kg washing machine.",
            "description": "<p>A freestanding washing machine.</p>", "short_description": "",
            "brand_label": "Beko", "ean": "8690842690662", "mpn": "BMN3WT3841S", "energy_label": "A",
            "height": "84.5", "width": "60", "depth": "54.6", "in_stock": True,
            "is_euronics": "0", "googleshopping_exclude": "0"}
    return {**base, **kw}


def _ld(**offer):
    good = {"@type": "Offer", "price": "329.00", "availability": "https://schema.org/InStock",
            "shippingDetails": {"shippingRate": {"value": 0, "currency": "GBP"},
                                "shippingDestination": {"addressCountry": "GB"}}}
    return {"@context": "https://schema.org", "@type": "Product", "name": "Beko BMN3WT3841S",
            "brand": {"@type": "Brand", "name": "Beko"}, "gtin13": "8690842690662",
            "aggregateRating": {"ratingValue": 4.8, "reviewCount": 12},
            "offers": {k: v for k, v in {**good, **offer}.items() if v is not None}}


def _page(ld, image="https://loshop.co.uk/media/catalog/product/b/m/bmn.jpg"):
    return (f'<html><head><title>Beko</title><meta property="og:title" content="Beko">'
            f'<meta property="og:description" content="8kg washer">'
            f'<meta property="og:image" content="{image}">'
            f'<script type="application/ld+json">{json.dumps(ld, indent=2)}</script></head></html>')


@pytest.mark.parametrize("code,ok", [("8690842690662", True), ("6901101815507", True),
                                     ("8690842644450", True), ("8690842690663", False),
                                     ("869084269066", False), ("ABC", False), ("", False),
                                     ("96385074", True)])
def test_gtin_check_digit(code, ok):
    assert jobs.gtin_ok(code) is ok


def test_complete_product_has_no_attribute_issues():
    assert jobs.check_attributes(_ai()) == []


@pytest.mark.parametrize("product,issue", [
    (_ai(brand_label=" "), "no brand"),
    (_ai(ean=""), "no GTIN"),
    (_ai(ean="8690842690663"), "GTIN invalid"),
    (_ai(mpn="  "), "no MPN"),
    (_ai(mpn="BMN3WY3841S"), "MPN does not match model"),   # seen live, 2026-10-16
    (_ai(price=0), "no price"),
    (_ai(in_stock=None), "no stock record"),
    (_ai(energy_label=""), "no energy rating"),
    (_ai(depth=""), "no dimensions"),
    (_ai(googleshopping_exclude="1"), "excluded from Google Shopping"),
])
def test_attribute_issues(product, issue):
    assert issue in [i for i, _ in jobs.check_attributes(product)]


def test_attribute_checks_avoid_false_alarms():
    # energy rating and size only where they apply; a Euronics item is meant to be excluded;
    # a configurable product has no price of its own; spec_* sizes count as dimensions
    for name in ("Hotpoint Washing Machine Hose Kit", "Morphy Richards Slow Cooker 6.5L",
                 "Philips Hair Dryer"):
        assert jobs.check_attributes(_ai(name=name, sku="ACC1", mpn="ACC1", energy_label="",
                                         depth="")) == []
    assert jobs.check_attributes(_ai(googleshopping_exclude="1", is_euronics="1")) == []
    assert jobs.check_attributes(_ai(type_id="configurable", price=0)) == []
    assert jobs.check_attributes(_ai(height="", width="", depth="", spec_height="85 cm",
                                     spec_width="60 cm", spec_depth="55 cm")) == []
    assert "no energy rating" not in [i for i, _ in jobs.check_attributes(
        _ai(energy_label="", euronics_energy_label="<a href=x>label</a>"))]
    assert "no dimensions" in [i for i, _ in jobs.check_attributes(
        _ai(name="Panasonic Microwave 25L", sku="NN1", mpn="NN1", depth=""))]


def test_product_ld_finds_the_product_node():
    product = {"@type": ["Product", "Thing"], "name": "X"}
    pages = ['<script type="application/ld+json">{not json</script>'
             f'<script type="application/ld+json">{json.dumps({"@type": "BreadcrumbList"})}</script>'
             f'<script type="application/ld+json">{json.dumps(product)}</script>',
             f'<script type="application/ld+json">{json.dumps({"@graph": [{"@type": "WebSite"}, product]})}</script>',
             f'<SCRIPT TYPE="application/ld+json">{json.dumps([product])}</SCRIPT>']
    for page in pages:
        assert jobs.product_ld(page) == product
    assert jobs.product_ld("<html></html>") is None


def test_good_structured_data_passes():
    assert jobs.check_structured_data(_ld(), _ai()) == []


@pytest.mark.parametrize("ld,product,issue", [
    (None, _ai(), "no Product JSON-LD"),
    ({**_ld(), "brand": {"@type": "Brand"}}, _ai(), "JSON-LD: no brand"),
    ({k: v for k, v in _ld().items() if k != "gtin13"}, _ai(), "JSON-LD: no GTIN"),
    ({k: v for k, v in _ld().items() if k != "offers"}, _ai(), "JSON-LD: no offer"),
    (_ld(price=None), _ai(), "JSON-LD: no price"),
    (_ld(price="349.00"), _ai(), "JSON-LD: price differs from catalogue"),
    (_ld(availability=None), _ai(), "JSON-LD: no availability"),
    (_ld(), _ai(in_stock=False), "JSON-LD: availability differs from stock"),
    (_ld(shippingDetails=None), _ai(), "JSON-LD: no delivery info"),
    (_ld(), _ai(is_euronics="1"), "JSON-LD: free delivery on a Euronics item"),
    (_ld(), _ai(ni_delivery_only="1"), "JSON-LD: delivery area too wide"),
    ({k: v for k, v in _ld().items() if k != "aggregateRating"}, _ai(), "JSON-LD: no rating"),
])
def test_structured_data_issues(ld, product, issue):
    assert issue in [i for i, _ in jobs.check_structured_data(ld, product)]


def test_structured_data_accepts_special_price_and_regional_delivery():
    assert jobs.check_structured_data(_ld(price="299"), _ai(special_price="299.0000")) == []
    regional = _ld(shippingDetails={"shippingRate": {"value": 30},
                                    "shippingDestination": {"addressCountry": "GB", "addressRegion": "NIR"}})
    assert jobs.check_structured_data(regional, _ai(ni_delivery_only="1", is_euronics="1")) == []
    assert jobs.check_structured_data(_ld(availability="https://schema.org/OutOfStock"),
                                      _ai(in_stock=False)) == []


def test_ai_crawlers_in_the_live_robots_file_are_allowed():
    blocked, others = jobs.check_ai_crawlers(LIVE_ROBOTS, "https://loshop.co.uk/beko.html")
    assert blocked == [] and others == ["Applebot-Extended", "Bytespider"]


def test_a_blocked_ai_crawler_is_found():
    robots = LIVE_ROBOTS.replace("User-agent: GPTBot\nAllow: /", "User-agent: GPTBot\nDisallow: /")
    robots += "\nUser-agent: PerplexityBot\nDisallow: /\n"
    blocked, _ = jobs.check_ai_crawlers(robots, "https://loshop.co.uk/beko.html")
    assert blocked == ["GPTBot", "PerplexityBot"]
    assert jobs.check_ai_crawlers("User-agent: *\nDisallow: /\n")[0] == jobs.AI_CRAWLERS


@pytest.mark.parametrize("status,text,needle", [
    (404, "", "not found"), (200, "<html>One moment</html>", "web page"),
    (200, "Lunneys " * 40, "'# ' title"), (200, "# Lunneys\n", "very short")])
def test_llms_txt_problems(status, text, needle):
    assert any(needle in p for p in jobs.check_llms(status, text))


def test_llms_txt_present():
    assert jobs.check_llms(200, LLMS) == []


def test_fetch_catalogue_asks_for_enabled_visible_products():
    seen = []

    def get(path):
        seen.append(path)
        return {"items": [{"sku": "a", "name": "A", "price": 5, "visibility": 4, "custom_attributes": [
                    {"attribute_code": "ean", "value": "123"}]},
                          {"sku": "h", "name": "H", "visibility": 1}], "total_count": 2}
    products = jobs.fetch_catalogue({"base": "x", "token": "t"}, get=get)
    assert [p["sku"] for p in products] == ["a"] and products[0]["ean"] == "123"
    assert seen[0].startswith("/rest/all/V1/products?") and "conditionType%5D=neq" in seen[0]


def test_sample_is_repeatable_and_keeps_euronics_apart():
    products = [_ai(sku=f"S{i}", is_euronics="1" if i % 3 == 0 else "0") for i in range(60)]
    scored, euronics = jobs.pick_ai_sample(products, 8, "2026-10-16")
    assert len(scored) == 8 and len(euronics) == 5
    assert not any(jobs._is_euronics(p) for p in scored) and all(jobs._is_euronics(p) for p in euronics)
    assert jobs.pick_ai_sample(products, 8, "2026-10-16") == (scored, euronics)


def test_top_fixes_rank_by_weight_times_products():
    rows = [{"sku": str(i), "issues": {"Structured data": [("JSON-LD: no rating", "")],
                                       "Key attributes": [("no GTIN", "")] if i < 2 else []}}
            for i in range(5)]
    fixes = jobs.top_ai_fixes(rows, ["robots.txt blocks GPTBot"])
    assert [f["issue"] for f in fixes] == ["robots.txt blocks GPTBot", "no GTIN", "JSON-LD: no rating"]
    assert fixes[1]["count"] == 2 and fixes[1]["skus"] == ["0", "1"]


def _ai_config(tmp_path):
    ini = tmp_path / "scripts.ini"
    ini.write_text("[magento]\nbase_url = https://loshop.co.uk\ntoken = SECRET-TOKEN\n", encoding="utf-8")
    config = tmp_path / "c.json"
    config.write_text(json.dumps({"magento_api": {"config_ini": str(ini)}}), encoding="utf-8")
    return config


def test_ai_readiness_needs_config_and_live(tmp_path):
    _, result, _ = _run(tmp_path, "run", "ai-readiness", "--live")
    assert result["status"] == "NEEDS_YOU" and "Not configured" in result["summary"]
    _, result, _ = _run(tmp_path, "run", "ai-readiness", config=_ai_config(tmp_path))
    assert result["status"] == "NEEDS_YOU" and "--live" in result["next_step"]


def _fake_site(monkeypatch, pages, catalogue):
    monkeypatch.setattr(jobs, "_fetch_bytes", lambda url, limit=0: (200, "image/png", _png(1200, 630)))
    catalogue = [{**p, "brand": "220", "energy_rating": "337"} for p in catalogue]
    monkeypatch.setattr(jobs, "fetch_catalogue", lambda api: catalogue)
    labels = {"brand": {"220": "Beko"}, "energy_rating": {"337": "A"}}
    monkeypatch.setattr(jobs, "attribute_labels", lambda api, code: labels[code])
    monkeypatch.setattr(jobs, "stock_status", lambda api, sku: True)


def test_ai_readiness_job_writes_report_and_csv(tmp_path, monkeypatch):
    catalogue = [_ai(sku="GOOD1", url_key="good1"),
                 _ai(sku="=BMN3WT3841S", name="=HYPERLINK(1) Beko BMN3WT3841S Washing Machine",
                     url_key="bad2", ean="",
                     mpn="BMN3WY3841S"),
                 _ai(sku="EUR1", url_key="eur1", is_euronics="1", googleshopping_exclude="1",
                     ni_delivery_only="1", ean="")]
    no_rating = {k: v for k, v in _ld().items() if k != "aggregateRating"}
    pages = {"https://loshop.co.uk/good1.html": _page(_ld()),
             "https://loshop.co.uk/bad2.html": _page(no_rating,
                                                     image="https://loshop.co.uk/media/logo/stores/1/logo.png"),
             "https://loshop.co.uk/eur1.html": _page(_ld()),
             "https://loshop.co.uk/robots.txt": LIVE_ROBOTS, "https://loshop.co.uk/llms.txt": LLMS}
    fetched, sleeps = [], []
    bot_once = {"https://loshop.co.uk/good1.html"}

    def fake_fetch(url, limit=0):
        fetched.append(url)
        if url in bot_once:                         # the host's bot check answers once
            bot_once.discard(url)
            return 200, "<html><title>One moment, please...</title></html>"
        return (200, pages[url]) if url in pages else (404, "")
    monkeypatch.setattr(jobs, "_fetch", fake_fetch)
    monkeypatch.setattr(jobs.time, "sleep", sleeps.append)
    _fake_site(monkeypatch, pages, catalogue)
    code, result, intray = _run(tmp_path, "run", "ai-readiness", "--live", "--delay", "0",
                                "--sample", "5", config=_ai_config(tmp_path))
    assert code == 0 and result["status"] == "NEEDS_YOU", result
    assert result["summary"].startswith("AI shopping readiness ") and "across 2 sampled" in result["summary"]
    assert sleeps == [20.0] and fetched.count("https://loshop.co.uk/good1.html") == 2
    md = (intray / "ai-readiness.md").read_text(encoding="utf-8")
    assert "| Key attributes | 50 | 1 of 2 products |" in md and "| Crawler access (site) | 100 |" in md
    assert "Key attributes: no GTIN** (1 of 2 products)" in md
    assert "Key attributes: MPN does not match model" in md
    assert "Share image: share image is the store logo" in md
    assert "Applebot-Extended" in md and "llms.txt: present" in md
    euronics = md.split("## Euronics agency items")[1]
    assert "JSON-LD: free delivery on a Euronics item: 1" in euronics and "no GTIN: 1" in euronics
    text = (intray / "ai-readiness.csv").read_text(encoding="utf-8")
    assert "'=HYPERLINK" in text and "'=BMN3WT3841S" in text and "Euronics (not scored)" in text
    assert "SECRET-TOKEN" not in text + md + json.dumps(result)


def test_ai_readiness_alerts_when_robots_blocks_an_ai_crawler(tmp_path, monkeypatch):
    pages = {"https://loshop.co.uk/good1.html": _page(_ld()),
             "https://loshop.co.uk/robots.txt": LIVE_ROBOTS + "\nUser-agent: OAI-SearchBot\nDisallow: /\n"}
    monkeypatch.setattr(jobs, "_fetch", lambda url, limit=0: (200, pages[url]) if url in pages else (404, ""))
    _fake_site(monkeypatch, pages, [_ai(sku="GOOD1", url_key="good1")])
    code, result, _ = _run(tmp_path, "run", "ai-readiness", "--live", "--delay", "0",
                           config=_ai_config(tmp_path))
    assert code == 1 and result["status"] == "ALERT"
    assert any("robots.txt blocks OAI-SearchBot" in f for f in result["findings"])
    assert any("/llms.txt not found" in f for f in result["findings"])
