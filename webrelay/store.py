import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .browser import BrowserResult


class Conflict(Exception):
    pass


FIELDS = "id,template,payload,status,attempts,run_at,due,history,result,error,created"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT NOT NULL,
                    template TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    run_at REAL NOT NULL, due REAL NOT NULL, history TEXT NOT NULL, result TEXT, screenshot BLOB,
                    error TEXT, lease TEXT, lease_until REAL NOT NULL DEFAULT 0, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, due, lease_until, created);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, template, payload, run_at, key):
        serialized = json.dumps(payload, sort_keys=True)
        fingerprint = hashlib.sha256(serialized.encode()).hexdigest()
        now = time.time()
        trace = [{"event": "job_scheduled", "at": now}]
        with self.connect() as db:
            row = db.execute(f"""
                INSERT INTO jobs(id,request_key,fingerprint,template,payload,status,run_at,due,history,created)
                VALUES(?,?,?,?,?,'queued',?,?,?,?) ON CONFLICT(request_key) DO NOTHING RETURNING {FIELDS}
            """, (str(uuid4()), key, fingerprint, template, serialized, run_at, run_at, json.dumps(trace), now)).fetchone()
            if row is None:
                row = db.execute(f"SELECT {FIELDS}, fingerprint FROM jobs WHERE request_key=?", (key,)).fetchone()
                if row["fingerprint"] != fingerprint:
                    raise Conflict("request_key_conflict")
            return self.view(row)

    def list(self):
        with self.connect() as db:
            return [self.view(row) for row in db.execute(f"SELECT {FIELDS} FROM jobs ORDER BY created DESC LIMIT 100")]

    def get(self, ident):
        with self.connect() as db:
            return self.view(db.execute(f"SELECT {FIELDS} FROM jobs WHERE id=?", (ident,)).fetchone())

    def screenshot(self, ident):
        with self.connect() as db:
            row = db.execute("SELECT screenshot FROM jobs WHERE id=? AND status='completed'", (ident,)).fetchone()
            return row["screenshot"] if row else None

    def claim(self):
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(f"""
                SELECT {FIELDS} FROM jobs WHERE ((status='queued' OR status='retrying') AND due<=?)
                OR (status='running' AND lease_until<=?) ORDER BY due,created LIMIT 1
            """, (now, now)).fetchone()
            if row is None:
                return None
            job = self.view(row)
            if job["attempts"] >= 3:
                self.append(job, "lease_exhausted")
                db.execute("UPDATE jobs SET status='failed',error='lease_exhausted',lease=NULL,history=? WHERE id=?",
                           (json.dumps(job["history"]), job["id"]))
                return None
            old = job["status"]
            job.update(status="running", attempts=job["attempts"] + 1)
            self.append(job, "worker_recovered" if old == "running" else "browser_started")
            lease = str(uuid4())
            db.execute("UPDATE jobs SET status='running',attempts=?,history=?,lease=?,lease_until=? WHERE id=?",
                       (job["attempts"], json.dumps(job["history"]), lease, now + 45, job["id"]))
            return job, lease

    def finish(self, job, lease, result: BrowserResult | None, *, error=None, retryable=False):
        now = time.time()
        retry = retryable and job["attempts"] < 3
        status = "completed" if result else "retrying" if retry else "failed"
        info = {"rows": result.rows, "duration_ms": result.duration_ms, "source": result.source} if result else None
        self.append(job, "evidence_saved" if result else "retry_scheduled" if retry else "job_failed", code=error)
        with self.connect() as db:
            return db.execute("""
                UPDATE jobs SET status=?,history=?,result=?,screenshot=?,error=?,due=?,lease=NULL,lease_until=0
                WHERE id=? AND status='running' AND lease=? AND lease_until>?
            """, (status, json.dumps(job["history"]), json.dumps(info) if result else None,
                  result.screenshot if result else None, error, now + (2 ** (job["attempts"] - 1) if retry else 0),
                  job["id"], lease, now)).rowcount == 1

    def cancel(self, ident):
        with self.connect() as db:
            row = db.execute(f"""
                UPDATE jobs SET status='cancelled',lease=NULL,lease_until=0,
                    history=json_insert(history,'$[#]',json_object('event','job_cancelled','at',?))
                WHERE id=? AND status IN ('queued','retrying','running') RETURNING {FIELDS}
            """, (time.time(), ident)).fetchone()
            if row:
                return self.view(row)
            existing = self.view(db.execute(f"SELECT {FIELDS} FROM jobs WHERE id=?", (ident,)).fetchone())
            if existing and existing["status"] != "cancelled":
                raise Conflict("cannot_cancel_completed_job")
            return existing

    @staticmethod
    def append(job, event, **details):
        job["history"].append({"event": event, "at": time.time(), "attempt": job["attempts"], **details})

    @staticmethod
    def view(row):
        if row is None:
            return None
        job = {key: row[key] for key in FIELDS.split(",")}
        for key in ("payload", "history", "result"):
            if job[key] is not None:
                job[key] = json.loads(job[key])
        return job
