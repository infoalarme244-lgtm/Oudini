"""File de tâches durable en base. Worker : `python -m app.worker`. Mode inline pour les tests."""
import logging
import traceback

from sqlalchemy import select

from ..config import settings
from ..models import Job, now

log = logging.getLogger("shopforge.jobs")
HANDLERS: dict = {}


def handler(kind):
    def deco(fn):
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(db, shop_id: int, kind: str, payload: dict) -> Job:
    job = Job(shop_id=shop_id, kind=kind, payload=payload)
    db.add(job)
    db.flush()
    if settings.jobs_inline:
        run(db, job)
    return job


def run(db, job: Job):
    _load_handlers()
    job.status, job.started_at, job.attempts = "running", now(), job.attempts + 1
    db.flush()
    try:
        job.result = HANDLERS[job.kind](db, job) or {}
        job.status = "done"
    except Exception as exc:
        log.error("job failed", extra={"job_id": job.id, "kind": job.kind})
        job.status, job.error = "failed", f"{exc}\n{traceback.format_exc()[-2000:]}"
    job.finished_at = now()
    db.commit()


def run_pending(db, limit=10) -> int:
    jobs = db.scalars(select(Job).where(Job.status == "queued", Job.run_after <= now()).order_by(Job.id).limit(limit)).all()
    for j in jobs:
        run(db, j)
    return len(jobs)


def _load_handlers():
    from . import analysis, catalog, imagegen, inventory, watch  # noqa: F401  (enregistrent leurs handlers)
