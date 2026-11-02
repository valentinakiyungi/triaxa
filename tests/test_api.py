import asyncio

from fastapi.testclient import TestClient

from app.llm import LLMError, MockAnalyzer
from app.main import create_app
from app.schemas import AnalysisResult
from app.store import JobStore

from .conftest import make_settings, wait_for


class FlakyAnalyzer(MockAnalyzer):
    """Fails the first `failures` calls, then behaves like the mock."""

    name = "flaky"

    def __init__(self, failures: int, retryable: bool = True):
        super().__init__()
        self.failures, self.retryable, self.calls = failures, retryable, 0

    async def analyze(self, text: str) -> AnalysisResult:
        self.calls += 1
        if self.calls <= self.failures:
            raise LLMError(f"boom {self.calls}", retryable=self.retryable)
        return await super().analyze(text)


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "healthy"
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["llm"]["provider"] == "mock"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Triaxa" in r.text


def test_analyze_sync(client):
    r = client.post("/analyze", json={"text": "I was charged twice on my invoice, this is unacceptable"})
    assert r.status_code == 200
    body = r.json()
    assert body["category"] == "Billing"
    assert body["urgency"] == "High"
    assert body["sentiment"] == "Negative"
    assert body["summary"] and body["action_item"]


def test_analyze_validation(client, settings):
    assert client.post("/analyze", json={"text": "   "}).status_code == 422
    assert client.post("/analyze", json={}).status_code == 422
    too_long = "x" * (settings.max_text_length + 1)
    assert client.post("/analyze", json={"text": too_long}).status_code == 413


def test_job_lifecycle(client):
    r = client.post("/jobs", json={"text": "Production is down for everyone!", "metadata": {"ticket": 7}})
    assert r.status_code == 202
    job = r.json()
    assert job["status"] == "queued"
    done = wait_for(client, job["id"])
    assert done["status"] == "completed"
    assert done["attempts"] == 1
    assert done["result"]["urgency"] == "Urgent"
    assert done["metadata"] == {"ticket": 7}
    assert done["duration_ms"] is not None

    # re-run
    r = client.post(f"/jobs/{job['id']}/retry")
    assert r.status_code == 202
    assert wait_for(client, job["id"])["status"] == "completed"

    # delete
    assert client.delete(f"/jobs/{job['id']}").status_code == 204
    assert client.get(f"/jobs/{job['id']}").status_code == 404
    assert client.delete(f"/jobs/{job['id']}").status_code == 404


def test_batch_list_filters_and_stats(client):
    texts = ["refund my payment please", "the app crashes on start", "love the product, thanks",
             "how do I change my username", "hello"]
    r = client.post("/jobs/batch", json={"items": [{"text": t} for t in texts]})
    assert r.status_code == 202
    ids = [j["id"] for j in r.json()]
    for i in ids:
        wait_for(client, i)

    all_jobs = client.get("/jobs").json()
    assert all_jobs["total"] == 5
    billing = client.get("/jobs", params={"category": "Billing"}).json()
    assert billing["total"] == 1 and billing["items"][0]["text"] == texts[0]
    page = client.get("/jobs", params={"limit": 2, "offset": 2}).json()
    assert len(page["items"]) == 2 and page["total"] == 5
    assert client.get("/jobs", params={"status": "bogus"}).status_code == 422

    stats = client.get("/stats").json()
    assert stats["total"] == 5
    assert stats["by_status"] == {"completed": 5}
    assert sum(stats["by_category"].values()) == 5
    assert stats["queue_depth"] == 0


def test_batch_limit(tmp_path):
    s = make_settings(tmp_path, max_batch_size=2)
    with TestClient(create_app(s, MockAnalyzer())) as c:
        r = c.post("/jobs/batch", json={"items": [{"text": "a"}, {"text": "b"}, {"text": "c"}]})
        assert r.status_code == 413


def test_api_key(tmp_path):
    s = make_settings(tmp_path, api_key="secret")
    with TestClient(create_app(s, MockAnalyzer())) as c:
        assert c.get("/health").status_code == 200  # health stays open
        assert c.post("/analyze", json={"text": "hi"}).status_code == 401
        assert c.get("/jobs", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/analyze", json={"text": "hi"}, headers={"X-API-Key": "secret"}).status_code == 200


def test_transient_failures_are_retried(tmp_path):
    s = make_settings(tmp_path, max_job_attempts=3)
    analyzer = FlakyAnalyzer(failures=2)
    with TestClient(create_app(s, analyzer)) as c:
        job = c.post("/jobs", json={"text": "app is slow"}).json()
        done = wait_for(c, job["id"])
        assert done["status"] == "completed"
        assert done["attempts"] == 3
        assert done["error"] is None


def test_retries_exhausted(tmp_path):
    s = make_settings(tmp_path, max_job_attempts=2)
    with TestClient(create_app(s, FlakyAnalyzer(failures=99))) as c:
        job = c.post("/jobs", json={"text": "x"}).json()
        done = wait_for(c, job["id"])
        assert done["status"] == "failed"
        assert done["attempts"] == 2
        assert "boom 2" in done["error"]


def test_non_retryable_fails_fast(tmp_path):
    analyzer = FlakyAnalyzer(failures=99, retryable=False)
    with TestClient(create_app(make_settings(tmp_path), analyzer)) as c:
        done = wait_for(c, c.post("/jobs", json={"text": "x"}).json()["id"])
        assert done["status"] == "failed" and done["attempts"] == 1
        assert analyzer.calls == 1
        # sync endpoint maps LLM errors to 502
        assert c.post("/analyze", json={"text": "x"}).status_code == 502


def test_interrupted_jobs_resume_on_startup(tmp_path):
    s = make_settings(tmp_path)

    async def seed():
        store = JobStore(s.database_path)
        job = await store.create("server crashed mid-way, error 500")
        await store.mark_processing(job.id)  # simulate a crash during processing
        queued = await store.create("still waiting in queue")
        await store.close()
        return job.id, queued.id

    interrupted, queued = asyncio.run(seed())
    with TestClient(create_app(s, MockAnalyzer())) as c:
        assert wait_for(c, interrupted)["status"] == "completed"
        assert wait_for(c, queued)["status"] == "completed"


def test_retry_conflict_and_missing(client):
    assert client.post("/jobs/nope/retry").status_code == 404
    assert client.get("/jobs/nope").status_code == 404


def test_search(client):
    texts = ["Refund my 50% discount please", "The app crashes on start", "crash_report attached"]
    ids = [j["id"] for j in client.post("/jobs/batch", json={"items": [{"text": t} for t in texts]}).json()]
    for i in ids:
        wait_for(client, i)
    hits = lambda q: {j["text"] for j in client.get("/jobs", params={"q": q}).json()["items"]}  # noqa: E731
    assert hits("CRASH") == {texts[1], texts[2]}
    assert hits("50%") == {texts[0]}          # % is literal, not a wildcard
    assert hits("crash_") == {texts[2]}       # _ is literal too
    assert hits("nothing like this") == set()
    assert client.get("/jobs", params={"q": "crash", "category": "Technical"}).json()["total"] == 2


def test_cors_preflight_allows_private_network(client):
    r = client.options("/jobs", headers={
        "Origin": "https://valentinakiyungi.github.io",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-api-key",
        "Access-Control-Request-Private-Network": "true",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-private-network"] == "true"
    assert r.headers["access-control-allow-origin"] in ("*", "https://valentinakiyungi.github.io")
