"""Job queue: Redis + RQ in production, an in-process thread for local testing.

Rendering never runs on the bot's event loop: with RQ it runs in a separate worker
process; the ``inline`` backend runs it on a background thread (one at a time), which is
enough to try the whole bot on one machine without Redis or Docker (including Windows,
where RQ's forking worker can't run).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from adreel.config import Settings
from adreel.log import get_logger

log = get_logger(__name__)

TASK_PATH = "adreel.jobs.tasks.run_project_job"


class JobQueue(Protocol):
    def enqueue(self, job_id: str) -> str | None: ...


class RQJobQueue:
    def __init__(self, s: Settings) -> None:
        from redis import Redis
        from rq import Queue

        self._s = s
        self._queue = Queue(s.queue_name, connection=Redis.from_url(s.redis_url))

    def enqueue(self, job_id: str) -> str | None:
        from rq import Retry

        retry = (
            Retry(max=self._s.job_retries, interval=[30, 120, 300][: max(1, self._s.job_retries)])
            if self._s.job_retries
            else None
        )
        job = self._queue.enqueue_call(
            TASK_PATH,
            args=(job_id,),
            timeout=self._s.job_timeout_s,
            retry=retry,
            job_id=f"adreel-{job_id}",
            result_ttl=3600,
            failure_ttl=7 * 24 * 3600,
        )
        return str(job.id)


class InlineJobQueue:
    """Runs jobs on one background thread in this process (dev / single machine)."""

    def __init__(self, s: Settings) -> None:
        self._s = s
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adreel-job")

    def enqueue(self, job_id: str) -> str | None:
        from adreel.jobs.tasks import run_project_job

        def _run() -> None:
            try:
                run_project_job(job_id, settings=self._s)
            except Exception:  # already recorded on the job; keep the thread alive
                log.exception("inline_job.crashed", job_id=job_id)

        self._pool.submit(_run)
        return None


def build_queue(s: Settings) -> JobQueue:
    return InlineJobQueue(s) if s.queue_backend == "inline" else RQJobQueue(s)
