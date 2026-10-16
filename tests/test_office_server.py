"""
office.server: the office as a local app with job buttons.

The real HTTP server runs on a random 127.0.0.1 port; the job runner is a stand-in,
so no test reaches the live server or website.
"""
from __future__ import annotations

import http.client
import threading
import time
from urllib.parse import urlencode

import pytest

from office import server as office_server
from office.jobs import JOBS


@pytest.fixture
def office(tmp_path):
    calls, gate = [], threading.Event()
    gate.set()

    def runner(argv):
        calls.append(argv)
        gate.wait(5)
        return 0, '[OK] done <script>alert("x")</script>'

    state = office_server.OfficeState(relay_db=tmp_path / "r.db", a2a_db=tmp_path / "a.db",
                                      intray=tmp_path / "intray", config=tmp_path / "c.json",
                                      port=0, runner=runner)
    srv = office_server.make_server(state)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"state": state, "calls": calls, "gate": gate, "port": state.port, "tmp": tmp_path}
    srv.shutdown()
    srv.server_close()


def request(office, method, path, body=None, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", office["port"], timeout=10)
    hdrs = {"Host": host or f"127.0.0.1:{office['port']}", **(headers or {})}
    if body is not None:
        body = urlencode(body)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    conn.request(method, path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp, data


def wait_idle(office):
    for _ in range(100):
        if office["state"].running is None and office["state"].last:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def run(office, job, live=False, token=None):
    body = {"token": token if token is not None else office["state"].token, "job": job}
    if live:
        body["live"] = "1"
    return request(office, "POST", "/run", body)


def test_page_has_a_button_per_job_and_safe_headers(office):
    resp, page = request(office, "GET", "/")
    assert resp.status == 200
    assert resp.getheader("X-Frame-Options") == "DENY"
    csp = resp.getheader("Content-Security-Policy")
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp and "script-src" not in csp
    assert "<script" not in page.lower()
    buttons = [b for bs in office_server.BUTTONS.values() for b in bs]
    assert page.count('action="/run"') == len(buttons)
    assert page.count('class="live"') == sum(1 for _, live in buttons if live)
    assert set(office_server.BUTTONS) == set(JOBS)
    assert f'value="{office["state"].token}"' in page


def test_other_hosts_are_refused(office):
    resp, _ = request(office, "GET", "/", host="evil.example:80")
    assert resp.status == 403
    resp, _ = request(office, "POST", "/run",
                      {"token": office["state"].token, "job": "category-images"},
                      host=f"attacker.example:{office['port']}")
    assert resp.status == 403 and office["calls"] == []


@pytest.mark.parametrize("token", ["", "wrong", "x" * 43])
def test_wrong_token_runs_nothing(office, token):
    resp, page = run(office, "category-images", token=token)
    assert resp.status == 403 and "expired" in page and office["calls"] == []


def test_unknown_job_and_bad_mode_are_refused(office):
    assert run(office, "rm-rf")[0].status == 400
    assert run(office, "social-batch", live=True)[0].status == 400
    assert run(office, "crontab-guard", live=False)[0].status == 400
    assert office["calls"] == []


def test_missing_or_odd_host_is_refused(office):
    for host in ("", "a.localhost:1", "LOCALHOST", f"localhost.:{office['port']}"):
        assert request(office, "GET", "/", host=host or " ")[0].status == 403


def test_too_many_fields_is_a_400_not_a_dropped_connection(office):
    body = {"token": office["state"].token, "job": "social-batch",
            **{f"f{i}": "x" for i in range(20)}}
    assert request(office, "POST", "/run", body)[0].status == 400
    assert office["calls"] == []


def test_a_second_office_cannot_share_the_port(office):
    state = office_server.OfficeState(relay_db=office["tmp"] / "r", a2a_db=office["tmp"] / "a",
                                      intray=office["tmp"] / "i", config=office["tmp"] / "c",
                                      port=office["port"])
    with pytest.raises(OSError):
        office_server.make_server(state)


def test_category_audit_gets_a_long_timeout(tmp_path):
    seen = []
    state = office_server.OfficeState(relay_db=tmp_path / "r", a2a_db=tmp_path / "a",
                                      intray=tmp_path / "i", config=tmp_path / "c")
    orig = office_server._subprocess_runner
    office_server._subprocess_runner = lambda argv, timeout=0: (seen.append(timeout) or (0, ""))
    try:
        state._run(state.job_argv("category-images", True), office_server.JOB_TIMEOUTS["category-images"])
    finally:
        office_server._subprocess_runner = orig
    assert seen == [3600]


def test_oversized_or_bad_body_is_refused(office):
    resp, _ = request(office, "POST", "/run", {"token": "a" * 5000, "job": "x"})
    assert resp.status == 400 and office["calls"] == []


def test_run_starts_the_job_and_shows_escaped_output(office):
    resp, _ = run(office, "category-images", live=True)
    assert resp.status == 303 and resp.getheader("Location") == "/"
    wait_idle(office)
    argv = office["calls"][0]
    assert argv[argv.index("run") + 1] == "category-images" and argv[-1] == "--live"
    assert argv[argv.index("--config") + 1] == str(office["tmp"] / "c.json")
    _, page = request(office, "GET", "/")
    assert "Last job: Category images backfill (CCP 3) (live)" in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in page and "<script>" not in page


def test_offline_run_has_no_live_flag(office):
    run(office, "social-batch")
    wait_idle(office)
    assert "--live" not in office["calls"][0]


def test_one_job_at_a_time_and_page_refreshes_while_busy(office):
    office["gate"].clear()
    assert run(office, "crontab-guard", live=True)[0].status == 303
    resp, page = run(office, "morning-briefing")
    assert resp.status == 409
    _, page = request(office, "GET", "/")
    assert 'http-equiv="refresh"' in page and "Working on: Crontab guard (live)" in page
    assert " disabled>" in page
    office["gate"].set()
    wait_idle(office)
    _, page = request(office, "GET", "/")
    assert 'http-equiv="refresh"' not in page and len(office["calls"]) == 1


def test_runner_crash_is_reported_not_fatal(office):
    office["state"].runner = lambda argv: (_ for _ in ()).throw(RuntimeError("boom"))
    run(office, "social-batch")
    wait_idle(office)
    assert office["state"].last["code"] == -1 and "boom" in office["state"].last["output"]
    assert request(office, "GET", "/")[0].status == 200


def test_unknown_paths_404(office):
    assert request(office, "GET", "/admin")[0].status == 404
    assert request(office, "POST", "/other", {"a": "b"})[0].status == 404


def test_server_binds_loopback_only(tmp_path):
    state = office_server.OfficeState(relay_db=tmp_path / "r", a2a_db=tmp_path / "a",
                                      intray=tmp_path / "i", config=tmp_path / "c", port=0)
    srv = office_server.make_server(state)
    try:
        assert srv.server_address[0] == "127.0.0.1"
    finally:
        srv.server_close()


def test_job_argv_uses_this_interpreter(tmp_path):
    state = office_server.OfficeState(relay_db=tmp_path / "r", a2a_db=tmp_path / "a",
                                      intray=tmp_path / "i", config=tmp_path / "c")
    argv = state.job_argv("crontab-guard", live=False)
    assert argv[:4] == [office_server.sys.executable, "-B", "-m", "office.jobs"]


def test_app_footer_is_not_the_snapshot_footer(office):
    _, page = request(office, "GET", "/")
    assert "The office app" in page and "This page is a snapshot" not in page


def test_retired_job_is_hidden_and_refused(office):
    (office["tmp"] / "c.json").write_text('{"retired_jobs": ["category-images"]}', encoding="utf-8")
    from office.jobs import save_result, JobResult
    save_result(office["state"].intray, JobResult(job="category-images", title="Category images backfill (CCP 3)",
                                                  desk="d", level="CHANGE", status="OK", summary="closed"))
    _, page = request(office, "GET", "/")
    assert "Category images backfill" not in page
    assert run(office, "category-images", live=True)[0].status == 400
    assert office["calls"] == []
