import asyncio
import csv
import hmac
import io
import logging
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from .browser import BrowserFault, BrowserRunner
from .store import Conflict, Store
from .templates import TEMPLATES, plan, sample_page

log = logging.getLogger(__name__)
DEMO_TOKEN = "webrelay-local-demo"


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    instruction: Annotated[str, Field(min_length=5, max_length=400)]
    search: Annotated[str, Field(max_length=80)] = ""
    scheduled_at: AwareDatetime | None = None
    simulate_first_failure: bool = False


def create_app(path: Path, token: str, origin: str, *, demo: bool = False, auto_worker: bool = True):
    if not demo and len(token) < 32:
        raise ValueError("Set a random OPERATOR_TOKEN of at least 32 characters.")
    store = Store(path)
    browser = BrowserRunner(origin)

    async def tick():
        claimed = store.claim()
        if claimed is None:
            return {"status": "idle"}
        job, lease = claimed
        template = TEMPLATES[job["template"]]
        try:
            result = await browser.retrieve(template, job["payload"]["search"], unavailable=(
                demo and job["payload"]["simulate_first_failure"] and job["attempts"] == 1
            ))
        except BrowserFault as error:
            saved = store.finish(job, lease, None, error=error.code, retryable=error.retryable)
            if saved:
                log.info("browser_failed job_id=%s attempt=%d code=%s", job["id"], job["attempts"], error.code)
        else:
            saved = store.finish(job, lease, result)
            if saved:
                log.info("browser_completed job_id=%s attempt=%d duration_ms=%d rows=%d",
                         job["id"], job["attempts"], result.duration_ms, len(result.rows))
        if not saved:
            log.info("browser_result_discarded job_id=%s attempt=%d", job["id"], job["attempts"])
        return store.get(job["id"])

    async def work():
        while True:
            try:
                await tick()
            except Exception as error:
                log.error("worker_unexpected kind=%s", type(error).__name__)
            await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(_app):
        worker = asyncio.create_task(work()) if auto_worker else None
        try:
            yield
        finally:
            if worker is not None:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker

    app = FastAPI(title="WebRelay", lifespan=lifespan)

    def authorize(authorization: Annotated[str | None, Header()] = None):
        supplied = (authorization or "").encode()
        if not hmac.compare_digest(supplied, f"Bearer {token}".encode()):
            raise HTTPException(401, "operator_required")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/config")
    def config():
        return {"demo": demo and token == DEMO_TOKEN}

    @app.get("/sample/{slug}", response_class=HTMLResponse)
    def fixture(slug: str, x_webrelay_demo_fault: Annotated[str | None, Header()] = None):
        if slug not in TEMPLATES:
            raise HTTPException(404, "sample_not_found")
        if demo and x_webrelay_demo_fault == "unavailable":
            return HTMLResponse("Simulated sample outage", status_code=503)
        return HTMLResponse(sample_page(TEMPLATES[slug]))

    api = APIRouter(prefix="/api", dependencies=[Depends(authorize)])

    @api.get("/templates")
    def templates():
        return [{"id": template.slug, "title": template.title, "fields": template.fields} for template in TEMPLATES.values()]

    @api.get("/jobs")
    def jobs():
        return store.list()

    @api.post("/jobs", status_code=202)
    def submit(body: Submission, key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=100)] = None):
        if body.simulate_first_failure and not demo:
            raise HTTPException(422, "simulation_requires_demo")
        try:
            template = plan(body.instruction)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        at = body.scheduled_at.timestamp() if body.scheduled_at is not None else time.time()
        try:
            return store.submit(template.slug, body.model_dump(mode="json"), at, key)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error

    @api.get("/jobs/{job_id}")
    def get(job_id: UUID):
        result = store.get(str(job_id))
        if result is None:
            raise HTTPException(404, "job_not_found")
        return result

    @api.post("/jobs/{job_id}/cancel")
    def cancel(job_id: UUID):
        try:
            job = store.cancel(str(job_id))
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        if job is None:
            raise HTTPException(404, "job_not_found")
        return job

    @api.get("/jobs/{job_id}/screenshot")
    def screenshot(job_id: UUID):
        data = store.screenshot(str(job_id))
        if data is None:
            raise HTTPException(404, "screenshot_unavailable")
        return Response(data, media_type="image/png")

    @api.get("/jobs/{job_id}/report")
    def report(job_id: UUID, format: Literal["csv", "json"] = "csv"):
        job = get(job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "report_unavailable")
        headers = {"Content-Disposition": f'attachment; filename="webrelay-{job_id}.{format}"'}
        if format == "json":
            return JSONResponse(job["result"], headers=headers)
        stream = io.StringIO(newline="")
        fields = TEMPLATES[job["template"]].fields
        writer = csv.writer(stream)
        writer.writerow(fields)
        for row in job["result"]["rows"]:
            writer.writerow([csv_cell(row[field]) for field in fields])
        return Response(stream.getvalue(), media_type="text/csv", headers=headers)

    @api.post("/worker/tick")
    async def manual_tick():
        return await tick()

    @app.middleware("http")
    async def security(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/sample/"):
            response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        elif not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'"
        return response

    app.include_router(api)
    app.mount("/", StaticFiles(directory=Path(__file__).with_name("static"), html=True), name="dashboard")
    return app


def csv_cell(value):
    return "'" + value if isinstance(value, str) and re.match(r"^[\s\ufeff]*[=+@-]", value) else value


def from_env():
    demo = os.getenv("DEMO_MODE") == "true"
    token = os.getenv("OPERATOR_TOKEN") or (DEMO_TOKEN if demo else "")
    return create_app(Path(os.getenv("DATABASE_PATH", "data/jobs.sqlite")), token,
                      os.getenv("SAMPLE_ORIGIN", "http://127.0.0.1:8000"), demo=demo)
