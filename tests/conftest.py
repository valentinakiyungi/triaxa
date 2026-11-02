import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.llm import MockAnalyzer  # noqa: E402


def make_settings(tmp_path, **overrides) -> Settings:
    base = dict(
        llm_provider="mock",
        database_path=str(tmp_path / "test.db"),
        retry_backoff_seconds=0.0,
        worker_concurrency=2,
        log_level="WARNING",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, MockAnalyzer())) as c:
        yield c


def wait_for(client, job_id, timeout=5.0, headers=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/jobs/{job_id}", headers=headers).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish: {job}")
