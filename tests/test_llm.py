import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.llm import (LLMError, MockAnalyzer, OpenAICompatibleAnalyzer, extract_json,
                     strict_json_schema)
from app.pipeline import Pipeline
from app.store import JobStore

GOOD = {"category": "Billing", "urgency": "Low", "sentiment": "Neutral",
        "summary": "Customer wants to upgrade from Free to Pro.", "action_item": "Send upgrade instructions."}


def settings(**kw) -> Settings:
    base = dict(llm_base_url="http://llm.test/v1", llm_model="test-model", llm_max_validation_retries=2)
    base.update(kw)
    return Settings(_env_file=None, **base)


def completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def make_analyzer(handler, **kw) -> OpenAICompatibleAnalyzer:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatibleAnalyzer(settings(**kw), client=client)


def run(coro):
    return asyncio.run(coro)


def test_strict_schema_is_flat():
    s = strict_json_schema()
    text = json.dumps(s)
    assert "$ref" not in text and "$defs" not in text
    assert s["additionalProperties"] is False
    assert set(s["required"]) == {"category", "urgency", "sentiment", "summary", "action_item"}
    assert s["properties"]["urgency"]["enum"] == ["Low", "Medium", "High", "Urgent"]


@pytest.mark.parametrize("raw", [
    json.dumps(GOOD),
    f"```json\n{json.dumps(GOOD)}\n```",
    f"Sure! Here is the analysis:\n{json.dumps(GOOD)}\nLet me know if you need more.",
])
def test_extract_json(raw):
    assert extract_json(raw) == GOOD


def test_success_sends_json_schema():
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    a = make_analyzer(handler)
    result = run(a.analyze("upgrade me to pro"))
    assert result.category.value == "Billing"
    body = seen["body"]
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.0
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][1] == {"role": "user", "content": "upgrade me to pro"}


def test_validation_errors_are_fed_back():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["messages"])
        if len(calls) == 1:
            return httpx.Response(200, json=completion('{"category": "Money", "urgency": "Low"}'))
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    result = run(make_analyzer(handler).analyze("hi"))
    assert result.summary == GOOD["summary"]
    assert len(calls) == 2
    retry_msgs = calls[1]
    assert retry_msgs[-2]["role"] == "assistant"
    assert "did not match the required schema" in retry_msgs[-1]["content"]


def test_validation_exhausted_raises():
    handler = lambda r: httpx.Response(200, json=completion("not json at all"))  # noqa: E731
    with pytest.raises(LLMError, match="failed validation"):
        run(make_analyzer(handler, llm_max_validation_retries=1).analyze("hi"))


def test_case_insensitive_enums():
    loose = {**GOOD, "category": "billing", "urgency": "HIGH", "sentiment": " negative "}
    handler = lambda r: httpx.Response(200, json=completion(json.dumps(loose)))  # noqa: E731
    result = run(make_analyzer(handler).analyze("hi"))
    assert (result.category.value, result.urgency.value, result.sentiment.value) == ("Billing", "High", "Negative")


def test_falls_back_when_response_format_unsupported():
    formats = []

    def handler(request):
        body = json.loads(request.content)
        fmt = body.get("response_format", {}).get("type")
        formats.append(fmt)
        if fmt in ("json_schema", "json_object"):
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    a = make_analyzer(handler)
    run(a.analyze("hi"))
    assert formats == ["json_schema", "json_object", None]
    assert a.mode == "prompt"
    run(a.analyze("again"))  # remembered: no more 400s
    assert formats[-1] is None and len(formats) == 4


def test_server_errors_are_retryable_client_errors_are_not():
    a = make_analyzer(lambda r: httpx.Response(503, text="loading model"))
    with pytest.raises(LLMError) as e:
        run(a.analyze("hi"))
    assert e.value.retryable

    a = make_analyzer(lambda r: httpx.Response(401, text="bad key"))
    a._mode_index = 2  # no response_format to fall back from
    with pytest.raises(LLMError) as e:
        run(a.analyze("hi"))
    assert not e.value.retryable


def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError) as e:
        run(make_analyzer(handler).analyze("hi"))
    assert e.value.retryable and "Cannot reach" in str(e.value)


def test_auto_model_resolution_and_health():
    used = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-3-8b"}, {"id": "other"}]})
        used.append(json.loads(request.content)["model"])
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    a = make_analyzer(handler, llm_model="auto")
    run(a.analyze("hi"))
    assert used == ["llama-3-8b"]
    h = run(a.health())
    assert h["ok"] and h["available_models"] == ["llama-3-8b", "other"]


def test_health_reports_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    h = run(make_analyzer(handler).health())
    assert h["ok"] is False and "refused" in h["error"]


@pytest.mark.parametrize("text,category,urgency", [
    ("Please refund the double charge", "Billing", "Medium"),
    ("I'm locked out of my account", "Account", "High"),
    ("Security breach! someone hacked us", "General", "Urgent"),
    ("Can I get a demo for my team?", "Sales", "Low"),
])
def test_mock_analyzer(text, category, urgency):
    r = run(MockAnalyzer().analyze(text))
    assert (r.category.value, r.urgency.value) == (category, urgency)


def test_webhook_called_on_completion(tmp_path):
    received = []

    def hook(request):
        received.append(json.loads(request.content))
        return httpx.Response(200)

    async def scenario():
        store = JobStore(str(tmp_path / "w.db"))
        s = Settings(_env_file=None, retry_backoff_seconds=0)
        p = Pipeline(store, MockAnalyzer(), s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(hook)))
        await p.start()
        job = await store.create("refund please", callback_url="http://hooks.test/done")
        await p.submit(job.id)
        await p.join()
        await p.stop()
        await store.close()
        return job.id

    job_id = run(scenario())
    assert len(received) == 1
    assert received[0]["id"] == job_id and received[0]["status"] == "completed"


@pytest.mark.parametrize("text", [
    "Our production dashboard has been down for 20 minutes and every API call returns 500.",
    "The checkout page is down!",
    "All of our customers see a 502 error",
])
def test_mock_flags_outages_as_urgent(text):
    assert run(MockAnalyzer().analyze(text)).urgency.value == "Urgent"
