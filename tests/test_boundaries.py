import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from webrelay.app import DEMO_TOKEN, create_app
from webrelay.browser import BrowserFault, BrowserResult, BrowserRunner
from webrelay.store import Store
from webrelay.templates import TEMPLATES, sample_page

AUTH = {"Authorization": "Bearer " + DEMO_TOKEN}
PAYLOAD = {"instruction": "Collect inventory", "search": "", "simulate_first_failure": False, "scheduled_at": None}


@contextmanager
def portal(mode):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Cookie")))
            if mode == "redirect" and self.path == "/sample/inventory":
                self.send_response(302)
                self.send_header("Location", "/outside")
                self.end_headers()
                return
            content = sample_page(TEMPLATES["inventory"])
            if mode == "resources":
                content = content.replace("</body>", '<img src="/outside"><iframe src="/outside"></iframe>'
                                          '<script>fetch("/outside")</script></body>')
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "session=must-not-survive; Path=/")
            self.end_headers()
            self.wfile.write(content.encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), seen
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_actual_chromium_blocks_redirect_before_fetching_another_path():
    with portal("redirect") as (origin, seen):
        with pytest.raises(BrowserFault, match="redirect_not_allowed") as error:
            asyncio.run(BrowserRunner(origin).retrieve(TEMPLATES["inventory"], ""))
        assert error.value.retryable is False
        assert [path for path, _ in seen] == ["/sample/inventory"]


def test_actual_chromium_blocks_subresources_and_isolates_session_cookies():
    with portal("resources") as (origin, seen):
        for _ in range(2):
            result = asyncio.run(BrowserRunner(origin).retrieve(TEMPLATES["inventory"], ""))
            assert len(result.rows) == 3
            assert result.screenshot.startswith(b"\x89PNG")
        assert seen == [("/sample/inventory", None), ("/sample/inventory", None)]


def test_cancellation_fences_inflight_browser_results_and_reports_discarded_evidence(tmp_path, monkeypatch, caplog):
    started, release = Event(), Event()

    async def retrieve(_runner, *_args, **_kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return BrowserResult([], b"stale screenshot", 1, "/sample/inventory")

    monkeypatch.setattr(BrowserRunner, "retrieve", retrieve)
    caplog.set_level(logging.INFO, logger="webrelay.app")
    with TestClient(create_app(tmp_path / "jobs.sqlite", DEMO_TOKEN, "http://127.0.0.1:8000", demo=True, auto_worker=False)) as client:
        job = client.post("/api/jobs", headers=AUTH, json=PAYLOAD).json()
        path = "/api/jobs/" + job["id"]
        with ThreadPoolExecutor(1) as executor:
            tick = executor.submit(client.post, "/api/worker/tick", headers=AUTH)
            try:
                assert started.wait(timeout=5)
                assert client.post(path + "/cancel", headers=AUTH).json()["status"] == "cancelled"
            finally:
                release.set()
            assert tick.result(timeout=5).json()["status"] == "cancelled"
        assert client.get(path + "/report", headers=AUTH).status_code == 409
        assert client.get(path + "/screenshot", headers=AUTH).status_code == 404
        assert client.get(path, headers=AUTH).json()["result"] is None
    assert any("browser_result_discarded" in record.message for record in caplog.records)
    assert not any("browser_completed" in record.message for record in caplog.records)


def test_future_schedule_survives_reopen_and_concurrent_workers_claim_once(tmp_path):
    path = tmp_path / "jobs.sqlite"
    store = Store(path)
    future = time.time() + 600
    job = store.submit("inventory", PAYLOAD, future, "future")
    reopened = Store(path)
    assert reopened.get(job["id"])["run_at"] == future
    assert reopened.claim() is None
    assert reopened.cancel(job["id"])["status"] == "cancelled"
    assert Store(path).get(job["id"])["status"] == "cancelled"
    current = store.submit("inventory", PAYLOAD, time.time(), "ready")
    with ThreadPoolExecutor(2) as executor:
        claims = list(executor.map(lambda repository: repository.claim(), [store, reopened]))
    assert sum(claim is not None for claim in claims) == 1
    claimed, lease = next(claim for claim in claims if claim is not None)
    assert claimed["id"] == current["id"]
    assert store.cancel(current["id"])["status"] == "cancelled"
    assert not reopened.finish(claimed, lease, BrowserResult([], b"stale", 1, "/sample/inventory"))
