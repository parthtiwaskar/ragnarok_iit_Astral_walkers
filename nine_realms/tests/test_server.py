"""Deployment protections: access token, trial limits, origin check, headers, public-bind guard. No LLM involved."""
from __future__ import annotations

import http.client
import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nine_realms import embed
from nine_realms.corpus import LocalCorpus
from nine_realms.server import App, Config, check_bind, default_pack, make_handler
from nine_realms.store import Store

PACK = default_pack()
pytestmark = pytest.mark.skipif(not (Path(PACK) / "manifest.json").exists(), reason="Runemark pack not found")
Q = {"question": "How exposed is Midgard wheat to the end of the Orvan corridor agreement?"}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    return LocalCorpus(PACK, tmp_path_factory.mktemp("c") / "c.sqlite")


class Srv:
    def __init__(self, corpus, **cfg):
        self.app = App(corpus, Store(), Config(token=cfg.pop("token", "secret-token"), **cfg), "anthropic")
        # a trial that finishes instantly, so the tests never touch an LLM
        def fake_run(case_id, *a):
            with self.app._lock:
                self.app._running.pop(case_id, None)
        self.app._run = fake_run
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.app.cfg.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.app.cfg.port, timeout=10)
        c.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), (json.loads(data) if data and r.getheader("Content-Type", "").startswith("application/json") else data)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture()
def srv(corpus):
    s = Srv(corpus)
    yield s
    s.close()


AUTH = {"Authorization": "Bearer secret-token"}


def test_health_is_public_but_reveals_nothing_without_the_token(srv):
    code, _, body = srv.call("GET", "/api/health")
    assert code == 200 and body["ok"] and body["auth_required"] is True and "provider" not in body and "running" not in body
    assert "provider" in srv.call("GET", "/api/health", headers=AUTH)[2]


def test_api_requires_the_access_token(srv):
    assert srv.call("GET", "/api/cases")[0] == 401
    assert srv.call("GET", "/api/documents?limit=1")[0] == 401
    assert srv.call("GET", "/api/cases", headers={"Authorization": "Bearer wrong"})[0] == 401
    assert srv.call("GET", "/api/cases", headers={"Authorization": "secret-token"})[0] == 401  # must be a Bearer header
    assert srv.call("POST", "/api/cases", Q)[0] == 401
    assert srv.call("GET", "/api/cases", headers=AUTH)[0] == 200
    assert srv.call("GET", "/api/documents?limit=1", headers=AUTH)[0] == 200


def test_the_dashboard_itself_loads_without_a_token_so_it_can_ask_for_one(srv):
    for path in ("/", "/app.js", "/charts.js", "/theme.js", "/styles.css"):
        assert srv.call("GET", path)[0] == 200


def test_no_token_configured_means_open_access_for_local_use(corpus):
    s = Srv(corpus, token="")
    try:
        assert s.call("GET", "/api/cases")[0] == 200
        assert s.call("GET", "/api/health")[2]["auth_required"] is False
    finally:
        s.close()


def test_security_headers_are_set(srv):
    _, h, _ = srv.call("GET", "/")
    assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff" and h["Referrer-Policy"] == "no-referrer"
    assert "script-src 'self'" in h["Content-Security-Policy"] and "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert "Server" in h and "Python" not in h["Server"]  # no interpreter version banner


def test_cross_origin_posts_are_refused(srv):
    assert srv.call("POST", "/api/cases", Q, {**AUTH, "Origin": "https://evil.example"})[0] == 403
    assert srv.call("POST", "/api/cases", Q, {**AUTH, "Origin": f"http://127.0.0.1:{srv.app.cfg.port}"})[0] == 202


def test_allowed_origins_can_be_configured(corpus):
    s = Srv(corpus, allowed_origins={"https://app.example.com"}, cooldown_s=0)
    try:
        assert s.call("POST", "/api/cases", Q, {**AUTH, "Origin": "https://app.example.com"})[0] == 202
        assert s.call("POST", "/api/cases", Q, {**AUTH, "Origin": "https://other.example.com"})[0] == 403
    finally:
        s.close()


def test_trials_are_rate_limited_per_client_and_per_day(corpus):
    s = Srv(corpus, cooldown_s=60, max_trials_per_day=2)
    try:
        assert s.call("POST", "/api/cases", Q, AUTH)[0] == 202
        code, headers, body = s.call("POST", "/api/cases", Q, AUTH)  # same client, inside the cooldown
        assert code == 429 and "wait" in body["error"] and int(headers["Retry-After"]) > 0
        s.app._last_start.clear()
        assert s.call("POST", "/api/cases", Q, AUTH)[0] == 202
        s.app._last_start.clear()
        code, _, body = s.call("POST", "/api/cases", Q, AUTH)  # daily cap of 2 reached
        assert code == 429 and "Daily trial limit" in body["error"]
    finally:
        s.close()


def test_a_busy_server_says_so_and_bad_input_is_rejected(corpus):
    s = Srv(corpus, cooldown_s=0)
    try:
        s.app._run = lambda *a: None  # trial never finishes, so the server stays busy
        assert s.call("POST", "/api/cases", Q, AUTH)[0] == 202
        assert s.call("POST", "/api/cases", Q, AUTH)[0] == 409
        assert s.call("POST", "/api/cases", {"question": "short"}, AUTH)[0] == 400
        assert s.call("POST", "/api/cases", {**Q, "as_of": "tomorrow"}, AUTH)[0] == 400
    finally:
        s.close()


def test_no_llm_key_gives_a_clear_503_not_a_crash(corpus):
    s = Srv(corpus)
    s.app.provider = ""
    try:
        code, _, body = s.call("POST", "/api/cases", Q, AUTH)
        assert code == 503 and "ANTHROPIC_API_KEY" in body["error"]
    finally:
        s.close()


def test_production_hides_internal_error_details(corpus):
    s = Srv(corpus, production=True)
    try:
        s.app.corpus = None  # force an internal failure on a protected route
        code, _, body = s.call("GET", "/api/summary", headers=AUTH)
        assert code == 500 and body == {"error": "internal error"}
    finally:
        s.close()


def test_refuses_to_listen_publicly_without_a_token(monkeypatch):
    monkeypatch.delenv("ALLOW_INSECURE_PUBLIC", raising=False)
    with pytest.raises(SystemExit) as e:
        check_bind("0.0.0.0", "")
    assert "APP_ACCESS_TOKEN" in str(e.value)
    check_bind("0.0.0.0", "a-token")  # fine with a token
    check_bind("127.0.0.1", "")       # fine on loopback
    monkeypatch.setenv("ALLOW_INSECURE_PUBLIC", "1")
    check_bind("0.0.0.0", "")         # explicit override


def test_config_reads_everything_from_the_environment(monkeypatch, tmp_path):
    for k, v in {"HOST": "0.0.0.0", "PORT": "9000", "DATA_DIR": str(tmp_path), "NINE_REALMS_ENV": "production", "APP_ACCESS_TOKEN": "t",
                 "ALLOWED_ORIGINS": "https://a.example.com/, https://b.example.com", "MAX_TRIALS_PER_DAY": "3", "TRIAL_COOLDOWN_SECONDS": "5",
                 "MAX_LLM_CALLS": "77", "MAX_CASE_TOKENS": "123456", "TRUST_PROXY": "1"}.items():
        monkeypatch.setenv(k, v)
    c = Config.from_env()
    assert (c.host, c.port, c.token, c.production, c.trust_proxy) == ("0.0.0.0", 9000, "t", True, True)
    assert c.db == str(tmp_path / "ragnarisk.sqlite") and c.memory_dir == str(tmp_path / "memory")
    assert c.allowed_origins == {"https://a.example.com", "https://b.example.com"}
    assert (c.max_trials_per_day, c.cooldown_s, c.max_llm_calls, c.max_case_tokens) == (3, 5, 77, 123456)


def test_head_requests_work_for_health_checks_and_return_no_body(srv):
    c = http.client.HTTPConnection("127.0.0.1", srv.app.cfg.port, timeout=10)
    c.request("HEAD", "/api/health")
    r = c.getresponse()
    assert r.status == 200 and r.read() == b"" and int(r.getheader("Content-Length")) > 0 and r.getheader("X-Frame-Options") == "DENY"


# ---------------------------------------------------------------------------------------------------------------------
# Live pack intake (final round): upload, drop folder, hostile archives, as-of parsing, concurrent trials
# ---------------------------------------------------------------------------------------------------------------------
import io
import zipfile

from nine_realms.livepacks import LivePacks
from nine_realms.rehearsal import make_pack


def zip_dir(path: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(path.rglob("*")):
            if p.is_file():
                z.write(p, f"{path.name}/{p.relative_to(path)}")  # wrapped in one folder, as zips usually are
    return buf.getvalue()


@pytest.fixture()
def live(tmp_path):
    os.environ["NINE_REALMS_EMBEDDER"] = "hash"
    embed._cached = None
    corpus = LocalCorpus(PACK, tmp_path / "c.sqlite")
    s = Srv(corpus)
    s.app.live = LivePacks(corpus, tmp_path / "live", 0)
    s.tmp = tmp_path
    yield s
    s.close()


def raw(srv, method, path, data=b"", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.app.cfg.port, timeout=30)
    c.request(method, path, data, {**AUTH, **(headers or {})})
    r = c.getresponse()
    body = r.read()
    return r.status, (json.loads(body) if body[:1] in (b"{", b"[") else body)


def test_a_zipped_pack_uploaded_live_is_searchable_and_reported(live):
    expect = make_pack(live.tmp / "pack_e9", source_pack=PACK)
    code, body = raw(live, "POST", "/api/ingest", zip_dir(live.tmp / "pack_e9"), {"X-Pack-Name": "pack_e9.zip", "Content-Type": "application/zip"})
    assert code == 201 and body["report"]["ingested"] >= 15 and "KET-0013" in body["report"]["quarantined"]
    packs = live.call("GET", "/api/packs", headers=AUTH)[2]["packs"]
    assert len(packs) == 2 and {p["event_id"] for p in packs} == {"E5", "E9"}
    found = live.call("GET", "/api/documents?prefix=KET&limit=100", headers=AUTH)[2]
    assert found["total"] >= 15 and any("TRUNCATED" in "".join(d["flags"]) for d in found["documents"])
    assert live.call("GET", f"/api/packs/{body['pack_id']}", headers=AUTH)[2]["report"]["trade"]["rows_flagged"]


def test_upload_needs_the_token_and_rejects_non_zip_input(live):
    assert live.call("POST", "/api/ingest")[0] == 401
    assert raw(live, "POST", "/api/ingest", b"this is not a zip")[0] == 400
    assert raw(live, "POST", "/api/ingest", b"")[0] == 400


def test_a_zip_slip_archive_is_refused_and_writes_nothing_outside(live):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../../escaped.txt", "DOC_ID: X\nPUB_DATE: 2027-01-01\n---\nhello" + "x" * 80)
    code, body = raw(live, "POST", "/api/ingest", buf.getvalue())
    assert code == 400 and "unsafe path" in body["error"]
    assert not (live.tmp.parent / "escaped.txt").exists() and not (live.tmp / "escaped.txt").exists()


def test_a_symlink_inside_an_archive_is_refused(live):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = (0o120777 << 16)
        z.writestr(info, "/etc/passwd")
    code, body = raw(live, "POST", "/api/ingest", buf.getvalue())
    assert code == 400 and "symbolic link" in body["error"]


def test_oversized_uploads_are_refused_before_they_are_read(live):
    live.app.cfg.max_upload_mb = 1
    try:
        assert raw(live, "POST", "/api/ingest", b"0" * (2 * 1024 * 1024))[0] == 413
    except ConnectionError:
        # the server refused without reading the body, so the client may see the connection close instead of the 413:
        # a reset or broken pipe on Linux, ConnectionAbortedError (WinError 10053) on Windows. All are ConnectionError.
        pass
    assert len(live.app.corpus.list_packs()) == 1  # nothing was ingested


def test_dropping_a_folder_or_a_zip_into_the_watched_directory_ingests_it(live):
    drop = live.tmp / "live"
    make_pack(drop / "pack_e9", source_pack=PACK)
    (drop / "pack_e9.zip").write_bytes(b"")  # an unrelated empty file must not break the scan
    code, body = raw(live, "POST", "/api/ingest/scan")
    assert code == 200 and body["ingested"] == ["pack-e9"]
    assert raw(live, "POST", "/api/ingest/scan")[1]["ingested"] == []  # unchanged packs are skipped on the next scan
    zip_pack = live.tmp / "z" / "pack_e7"
    make_pack(zip_pack, source_pack=PACK, event_id="E7")
    (drop / "second_pack.zip").write_bytes(zip_dir(zip_pack))
    body = raw(live, "POST", "/api/ingest/scan")[1]
    assert "second-pack" in body["ingested"]


def test_a_broken_pack_in_the_drop_folder_is_reported_and_does_not_stop_the_server(live):
    bad = live.tmp / "live" / "broken"
    (bad / "docs").mkdir(parents=True)
    (bad / "manifest.json").write_text("{not json", encoding="utf-8")
    (bad / "docs" / "a.txt").write_bytes(b"")
    code, body = raw(live, "POST", "/api/ingest/scan")
    assert code == 200
    assert live.call("GET", "/api/health")[0] == 200 and live.call("GET", "/api/summary", headers=AUTH)[0] == 200


def test_the_as_of_date_is_read_from_the_question(live):
    live.app.cfg.cooldown_s = 0
    code, _, body = live.call("POST", "/api/cases", {"question": "As of 12 March 2027, how exposed is Midgard to the Orvan corridor ending?"}, AUTH)
    assert code == 202
    assert live.app.store.load_case(body["case_id"])["as_of"] == "2027-03-12"
    code, _, body = live.call("POST", "/api/cases", {"question": "How exposed is Midgard to the Orvan corridor ending? (no date)"}, AUTH)
    assert live.app.store.load_case(body["case_id"])["as_of"] == live.app.corpus.pack_as_of  # falls back to the newest pack's date


def test_several_trials_can_run_at_once_when_allowed(corpus):
    s = Srv(corpus, cooldown_s=0, max_concurrent=2)
    try:
        s.app._run = lambda *a: None  # trials stay "running"
        assert [s.call("POST", "/api/cases", Q, AUTH)[0] for _ in range(3)] == [202, 202, 409]
        assert len(s.call("GET", "/api/health", headers=AUTH)[2]["running_ids"]) == 2
    finally:
        s.close()
