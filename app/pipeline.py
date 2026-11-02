"""Asynchronous job pipeline: an in-process queue drained by N worker tasks.

Jobs are persisted in SQLite before they are queued, so nothing is lost on a
restart — interrupted jobs are re-queued at startup.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, Set

import httpx

from .config import Settings
from .llm import Analyzer, LLMError
from .schemas import Job, JobStatus
from .store import JobStore

log = logging.getLogger(__name__)


class Pipeline:
    def __init__(self, store: JobStore, analyzer: Analyzer, settings: Settings,
                 http_client: Optional[httpx.AsyncClient] = None):
        self.store = store
        self.analyzer = analyzer
        self.settings = settings
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._timers: Set[asyncio.Task] = set()
        self._http = http_client or httpx.AsyncClient(timeout=10.0)
        self._in_flight = 0

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        recovered = await self.store.requeue_interrupted()
        for job_id in recovered:
            self.queue.put_nowait(job_id)
        if recovered:
            log.info("Re-queued %d pending job(s) from a previous run", len(recovered))
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"pipeline-worker-{i}")
            for i in range(self.settings.worker_concurrency)
        ]
        log.info("Pipeline started with %d worker(s) using the %r analyzer",
                 len(self._workers), self.analyzer.name)

    async def stop(self) -> None:
        for t in [*self._workers, *self._timers]:
            t.cancel()
        await asyncio.gather(*self._workers, *self._timers, return_exceptions=True)
        await self._http.aclose()

    @property
    def queue_depth(self) -> int:
        return self.queue.qsize() + self._in_flight

    async def submit(self, job_id: str) -> None:
        await self.queue.put(job_id)

    async def join(self) -> None:
        """Wait until every queued job (including scheduled retries) has finished. Used by tests."""
        while True:
            await self.queue.join()
            if not self._timers:
                return
            await asyncio.sleep(0.01)

    # -- worker -------------------------------------------------------------
    async def _worker(self, idx: int) -> None:
        while True:
            job_id = await self.queue.get()
            self._in_flight += 1
            try:
                await self._process(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a worker must never die
                log.exception("Worker %d crashed processing job %s", idx, job_id)
            finally:
                self._in_flight -= 1
                self.queue.task_done()

    async def _process(self, job_id: str) -> None:
        job = await self.store.get(job_id)
        if job is None or job.status not in (JobStatus.queued, JobStatus.processing):
            return  # deleted or already handled
        await self.store.mark_processing(job_id)
        attempt = job.attempts + 1
        started = time.perf_counter()
        try:
            result = await self.analyzer.analyze(job.text)
        except Exception as e:  # noqa: BLE001
            retryable = getattr(e, "retryable", True)
            message = str(e) if isinstance(e, LLMError) else f"{type(e).__name__}: {e}"
            final = (not retryable) or attempt >= self.settings.max_job_attempts
            await self.store.mark_failed(job_id, message, final=final)
            if final:
                log.warning("Job %s failed after %d attempt(s): %s", job_id, attempt, message)
                await self._notify(job_id)
            else:
                delay = self.settings.retry_backoff_seconds * (2 ** (attempt - 1))
                log.info("Job %s attempt %d failed (%s); retrying in %.1fs", job_id, attempt, message, delay)
                self._schedule(job_id, delay)
            return
        duration_ms = int((time.perf_counter() - started) * 1000)
        await self.store.mark_completed(job_id, result, duration_ms)
        log.info("Job %s completed in %d ms -> %s/%s", job_id, duration_ms,
                 result.category.value, result.urgency.value)
        await self._notify(job_id)

    def _schedule(self, job_id: str, delay: float) -> None:
        async def later() -> None:
            try:
                await asyncio.sleep(delay)
                await self.queue.put(job_id)
            finally:
                self._timers.discard(task)

        task = asyncio.create_task(later())
        self._timers.add(task)

    async def _notify(self, job_id: str) -> None:
        job: Optional[Job] = await self.store.get(job_id)
        if not job or not job.callback_url:
            return
        try:
            resp = await self._http.post(job.callback_url, content=job.model_dump_json(),
                                         headers={"Content-Type": "application/json"})
            log.info("Webhook for job %s -> %s (%d)", job_id, job.callback_url, resp.status_code)
        except httpx.HTTPError as e:
            log.warning("Webhook for job %s to %s failed: %s", job_id, job.callback_url, e)
