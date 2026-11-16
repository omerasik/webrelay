import re
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Thread

import pytest
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from webrelay.app import DEMO_TOKEN, create_app, csv_cell
from webrelay.browser import BrowserRunner
from webrelay.store import Store
from webrelay.templates import TEMPLATES, plan, validate_rows

AUTH = {"Authorization": f"Bearer {DEMO_TOKEN}"}
JOB = {"instruction": "Collect the inventory stock report.", "search": "filter"}


@contextmanager
def local_sample(tmp_path, *, worker=False):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        origin = "http://127.0.0.1:" + str(listener.getsockname()[1])
        app = create_app(tmp_path / "fixture.sqlite", DEMO_TOKEN, origin, demo=True, auto_worker=worker)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            for _ in range(300):
                if server.started:
                    break
                time.sleep(0.02)
            assert server.started
            yield origin
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def test_actual_chromium_recovers_from_503_and_saves_evidence_and_report(tmp_path):
    with local_sample(tmp_path) as origin:
        db = tmp_path / "jobs.sqlite"
        with TestClient(create_app(db, DEMO_TOKEN, origin, demo=True, auto_worker=False)) as client:
            response = client.post("/api/jobs", headers={**AUTH, "Idempotency-Key": "job-1"},
                                   json={**JOB, "simulate_first_failure": True})
            assert response.status_code == 202, response.text
            ident = response.json()["id"]
            path = "/api/jobs/" + ident
            assert client.get(path, headers=AUTH).json()["status"] == "queued"
            retry = client.post("/api/worker/tick", headers=AUTH).json()
            assert retry["status"] == "retrying", retry
            assert retry["error"] == "sample_unavailable"
            assert client.get(path + "/report", headers=AUTH).status_code == 409
            assert client.get(path + "/screenshot", headers=AUTH).status_code == 404
        time.sleep(1.05)
        with TestClient(create_app(db, DEMO_TOKEN, origin, demo=True, auto_worker=False)) as client:
            done = client.post("/api/worker/tick", headers=AUTH).json()
            assert done["status"] == "completed", done
            assert done["attempts"] == 2
            assert done["result"]["rows"] == [{"sku": "HX-068", "product": "Intake filter pack",
                                                "stock": 0, "updated": "2026-10-15"}]
            assert done["result"]["source"] == "/sample/inventory"
            assert done["result"]["duration_ms"] > 0
            assert [event["event"] for event in done["history"]] == [
                "job_scheduled", "browser_started", "retry_scheduled", "browser_started", "evidence_saved"
            ]
            evidence = client.get(path + "/screenshot", headers=AUTH)
            assert evidence.headers["Content-Type"] == "image/png"
            assert len(evidence.content) > 1024 and evidence.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert client.get(path + "/screenshot").status_code == 401
            csv = client.get(path + "/report?format=csv", headers=AUTH)
            assert csv.text.splitlines() == ["sku,product,stock,updated", "HX-068,Intake filter pack,0,2026-10-15"]
            assert client.get(path + "/report?format=json", headers=AUTH).json() == done["result"]


def test_future_jobs_cancellation_auth_and_idempotency(tmp_path):
    origin = "http://127.0.0.1:8000"
    with TestClient(create_app(tmp_path / "jobs.sqlite", DEMO_TOKEN, origin, demo=True, auto_worker=False)) as client:
        future = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        data = {**JOB, "scheduled_at": future}
        assert client.post("/api/jobs", json=data).status_code == 401
        headers = {**AUTH, "Idempotency-Key": "scheduled"}
        created = client.post("/api/jobs", json=data, headers=headers).json()
        assert client.post("/api/worker/tick", headers=AUTH).json()["status"] == "idle"
        assert client.post("/api/jobs", json=data, headers=headers).json()["id"] == created["id"]
        assert client.post("/api/jobs", json={**data, "search": "different"}, headers=headers).status_code == 409
        assert client.post("/api/jobs/" + created["id"] + "/cancel", headers=AUTH).json()["status"] == "cancelled"
        assert client.post("/api/jobs/" + created["id"] + "/cancel", headers=AUTH).status_code == 200
        assert client.post("/api/jobs", json={**JOB, "instruction": "Open https://elsewhere.test/ and load stock"}, headers=AUTH).status_code == 422


def test_expired_job_lease_is_reclaimed_and_stale_result_is_fenced(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    run = store.submit("inventory", JOB, time.time(), None)
    old, stale = store.claim()
    assert old["attempts"] == 1
    assert store.claim() is None
    with store.connect() as db:
        db.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (run["id"],))
    job, current = store.claim()
    assert job["attempts"] == 2
    assert job["history"][-1]["event"] == "worker_recovered"
    assert not store.finish(old, stale, None, error="stale_write")
    assert store.finish(job, current, None, error="sample_unavailable")
    assert store.get(run["id"])["error"] == "sample_unavailable"


def test_template_and_navigation_boundaries():
    assert plan("Read purchase order status.").slug == "orders"
    assert csv_cell(" =1+2") == "' =1+2"
    for attempt in ["https://example.test", "http://localhost:8000", "http://127.0.0.1:8000/other"]:
        with pytest.raises(ValueError, match="SAMPLE_ORIGIN"):
            BrowserRunner(attempt)
    with pytest.raises(ValueError, match="invalid_stock"):
        validate_rows(TEMPLATES["inventory"], [["BX-104", "Brackets", "-3", "2026-10-14"]], "")


def test_dashboard_submits_job_and_displays_captured_webpage(tmp_path):
    with local_sample(tmp_path, worker=True) as origin, sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(origin)
            page.get_by_role("button", name="Purchase order status").dispatch_event("click")
            page.get_by_label("Filter extracted rows (optional)").evaluate("(input) => { input.value = 'Leaf Works'; }")
            page.get_by_role("button", name="Schedule job").dispatch_event("click")
            expect(page.get_by_role("cell", name="PO-410")).to_be_visible(timeout=45_000)
            image = page.get_by_role("img", name="Captured sample webpage")
            expect(image).to_have_attribute("src", re.compile("^blob:"), timeout=10_000)
            with page.expect_download() as download:
                page.get_by_role("button", name="Download CSV").dispatch_event("click")
            assert download.value.suggested_filename.endswith(".csv")
            assert "PO-410,Leaf Works,processing" in download.value.path().read_text(encoding="utf-8")
        finally:
            browser.close()
