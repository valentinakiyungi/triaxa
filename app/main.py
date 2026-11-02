"""FastAPI application: REST API, job pipeline lifecycle and the web dashboard."""

from __future__ import annotations

import inspect
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader

from . import __title__, __version__
from .config import Settings, get_settings
from .llm import Analyzer, LLMError, build_analyzer
from .pipeline import Pipeline
from .schemas import (AnalysisResult, AnalyzeRequest, BatchCreate, Category, Job, JobCreate,
                      JobList, JobStatus, Stats, Urgency)
from .store import JobStore

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("app")


def create_app(settings: Optional[Settings] = None, analyzer: Optional[Analyzer] = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = JobStore(settings.database_path)
        llm = analyzer or build_analyzer(settings)
        pipeline = Pipeline(store, llm, settings)
        app.state.store, app.state.analyzer, app.state.pipeline = store, llm, pipeline
        await pipeline.start()
        try:
            yield
        finally:
            await pipeline.stop()
            await llm.aclose()
            await store.close()

    app = FastAPI(
        title="Triaxa",
        description="Structured triage of support tickets with local or hosted LLMs. "
        "Synchronous `/analyze` for one-off calls; `/jobs` for queued, persisted, retried processing.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    # The GitHub Pages dashboard is a public HTTPS site; browsers that enforce
    # Private Network Access only let it call a server on localhost / your LAN
    # if the server opts in. Newer Starlette has a flag for it; older versions
    # get the header from the small middleware below.
    cors_kwargs = {}
    if "allow_private_network" in inspect.signature(CORSMiddleware.__init__).parameters:
        cors_kwargs["allow_private_network"] = True
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
        **cors_kwargs,
    )

    @app.middleware("http")
    async def allow_private_network(request: Request, call_next):
        response = await call_next(request)
        if request.headers.get("access-control-request-private-network") == "true":
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def require_key(key: Optional[str] = Security(api_key_header)) -> None:
        if settings.api_key and key != settings.api_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key header")

    def check_length(text: str) -> None:
        if len(text) > settings.max_text_length:
            raise HTTPException(413,
                                f"text exceeds {settings.max_text_length} characters")

    def store(request: Request) -> JobStore:
        return request.app.state.store

    def pipeline(request: Request) -> Pipeline:
        return request.app.state.pipeline

    # -- UI & health --------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health", tags=["system"])
    async def health():
        """Liveness: the API process is up."""
        return {"status": "healthy", "service": __title__, "version": __version__}

    @app.get("/health/ready", tags=["system"])
    async def ready(request: Request):
        """Readiness: the LLM backend is reachable."""
        llm = await request.app.state.analyzer.health()
        body = {"status": "ready" if llm.get("ok") else "degraded", "llm": llm,
                "auth_required": bool(settings.api_key)}
        return JSONResponse(body, status_code=200 if llm.get("ok") else 503)

    # -- synchronous analysis ----------------------------------------------
    @app.post("/analyze", response_model=AnalysisResult, tags=["analysis"],
              dependencies=[Depends(require_key)])
    async def analyze(payload: AnalyzeRequest, request: Request):
        """Analyze one text and wait for the result (no queueing, no persistence)."""
        check_length(payload.text)
        try:
            return await request.app.state.analyzer.analyze(payload.text)
        except LLMError as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    # -- job pipeline -------------------------------------------------------
    @app.post("/jobs", response_model=Job, status_code=status.HTTP_202_ACCEPTED, tags=["jobs"],
              dependencies=[Depends(require_key)])
    async def create_job(payload: JobCreate, s: JobStore = Depends(store), p: Pipeline = Depends(pipeline)):
        """Queue a text for analysis. Poll `GET /jobs/{id}` or pass `callback_url` for a webhook."""
        check_length(payload.text)
        job = await s.create(payload.text, payload.metadata,
                             str(payload.callback_url) if payload.callback_url else None)
        await p.submit(job.id)
        return job

    @app.post("/jobs/batch", response_model=list[Job], status_code=status.HTTP_202_ACCEPTED,
              tags=["jobs"], dependencies=[Depends(require_key)])
    async def create_batch(payload: BatchCreate, s: JobStore = Depends(store), p: Pipeline = Depends(pipeline)):
        """Queue many texts at once."""
        if len(payload.items) > settings.max_batch_size:
            raise HTTPException(413,
                                f"batch exceeds {settings.max_batch_size} items")
        for item in payload.items:
            check_length(item.text)
        jobs = []
        for item in payload.items:
            job = await s.create(item.text, item.metadata, str(item.callback_url) if item.callback_url else None)
            await p.submit(job.id)
            jobs.append(job)
        return jobs

    @app.get("/jobs", response_model=JobList, tags=["jobs"], dependencies=[Depends(require_key)])
    async def list_jobs(
        status_: Optional[JobStatus] = Query(None, alias="status"),
        category: Optional[Category] = None,
        urgency: Optional[Urgency] = None,
        q: Optional[str] = Query(None, max_length=200, description="Search ticket text and summaries"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        s: JobStore = Depends(store),
    ):
        items, total = await s.list(
            status_.value if status_ else None,
            category.value if category else None,
            urgency.value if urgency else None,
            limit, offset, q.strip() if q and q.strip() else None,
        )
        return JobList(items=items, total=total, limit=limit, offset=offset)

    @app.get("/jobs/{job_id}", response_model=Job, tags=["jobs"], dependencies=[Depends(require_key)])
    async def get_job(job_id: str, s: JobStore = Depends(store)):
        job = await s.get(job_id)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
        return job

    @app.post("/jobs/{job_id}/retry", response_model=Job, status_code=status.HTTP_202_ACCEPTED,
              tags=["jobs"], dependencies=[Depends(require_key)])
    async def retry_job(job_id: str, s: JobStore = Depends(store), p: Pipeline = Depends(pipeline)):
        """Re-run a completed or failed job."""
        job = await s.get(job_id)
        if not job:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
        if job.status in (JobStatus.queued, JobStatus.processing):
            raise HTTPException(status.HTTP_409_CONFLICT, f"Job is already {job.status.value}")
        await s.reset_for_retry(job_id)
        await p.submit(job_id)
        return await s.get(job_id)

    @app.delete("/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["jobs"],
                dependencies=[Depends(require_key)])
    async def delete_job(job_id: str, s: JobStore = Depends(store)):
        if not await s.delete(job_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")

    @app.get("/stats", response_model=Stats, tags=["jobs"], dependencies=[Depends(require_key)])
    async def stats(s: JobStore = Depends(store), p: Pipeline = Depends(pipeline)):
        data = await s.stats()
        return Stats(**data, queue_depth=p.queue_depth, workers=settings.worker_concurrency)

    return app


app = create_app()
