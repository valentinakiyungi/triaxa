"""LLM analyzers.

``OpenAICompatibleAnalyzer`` talks to any server that implements the OpenAI
``/chat/completions`` API (LM Studio, Ollama, vLLM, llama.cpp server, OpenAI).
It asks for JSON-schema constrained output, validates the reply with Pydantic
and, when validation fails, feeds the errors back to the model and retries —
the same loop the Instructor library runs, without its dependency tree.

``MockAnalyzer`` is a deterministic keyword classifier for demos, CI and
running the service without a model.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Protocol

import httpx
from pydantic import ValidationError

from .config import Settings
from .schemas import AnalysisResult, Category, Sentiment, Urgency

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert AI operations assistant that triages customer support tickets.
Analyze the incoming text and extract structured triage metadata.
Rely ONLY on facts directly stated in the text. Do not assume, extrapolate, or invent details.

Respond with a single JSON object and nothing else, with exactly these fields:
- "category": one of {categories}
- "urgency": one of {urgencies} (Urgent = outage, data loss, security issue or legal/financial deadline; High = blocked from core use; Medium = degraded or time-sensitive; Low = questions, requests, feedback)
- "sentiment": one of {sentiments}
- "summary": one concise sentence describing the issue
- "action_item": the single most useful next step for support staff"""


def _enum_list(enum_cls) -> str:
    return ", ".join(f'"{e.value}"' for e in enum_cls)


def build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        categories=_enum_list(Category),
        urgencies=_enum_list(Urgency),
        sentiments=_enum_list(Sentiment),
    )


def strict_json_schema(model=AnalysisResult) -> Dict[str, Any]:
    """Pydantic JSON schema with $refs inlined and additionalProperties disabled.

    OpenAI's strict structured-output mode and several local servers reject
    $ref/$defs, so we flatten them.
    """
    schema = copy.deepcopy(model.model_json_schema())
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].split("/")[-1]
                merged = {**resolve(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
                return merged
            # Length limits are enforced by Pydantic; several servers reject them in strict mode.
            dropped = ("title", "default", "minLength", "maxLength")
            out = {k: resolve(v) for k, v in node.items() if k not in dropped}
            if out.get("type") == "object":
                out["additionalProperties"] = False
                out["required"] = list(out.get("properties", {}).keys())
            return out
        if isinstance(node, list):
            return [resolve(n) for n in node]
        return node

    return resolve(schema)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(content: str) -> Any:
    """Parse a JSON object out of a model reply that may contain fences or chatter."""
    text = content.strip()
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMError(Exception):
    """Raised when analysis fails. ``retryable`` tells the pipeline whether to try again."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class Analyzer(Protocol):
    name: str

    async def analyze(self, text: str) -> AnalysisResult: ...

    async def health(self) -> Dict[str, Any]: ...

    async def aclose(self) -> None: ...


class OpenAICompatibleAnalyzer:
    name = "openai"

    # Structured-output modes, most to least constrained. We step down when a
    # server rejects one, and remember what worked.
    _MODES = ("json_schema", "json_object", "prompt")

    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.base_url = settings.llm_base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=10.0),
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        )
        self._mode_index = 0
        self._model: Optional[str] = None if settings.llm_model in ("", "auto") else settings.llm_model
        self._model_lock = asyncio.Lock()
        self._schema = strict_json_schema()
        self._system_prompt = build_system_prompt()

    @property
    def mode(self) -> str:
        return self._MODES[self._mode_index]

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _list_models(self) -> List[str]:
        resp = await self._client.get(f"{self.base_url}/models", timeout=10.0)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", []) if m.get("id")]

    async def _resolve_model(self) -> str:
        if self._model:
            return self._model
        async with self._model_lock:
            if not self._model:
                try:
                    models = await self._list_models()
                except httpx.HTTPError as e:
                    raise LLMError(f"Cannot reach LLM server at {self.base_url}: {e}") from e
                if not models:
                    raise LLMError("LLM server reports no loaded models; load one or set LLM_MODEL.")
                self._model = models[0]
                log.info("LLM_MODEL=auto resolved to %r", self._model)
        return self._model

    def _response_format(self) -> Optional[Dict[str, Any]]:
        if self.mode == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {"name": "analysis_result", "strict": True, "schema": self._schema},
            }
        if self.mode == "json_object":
            return {"type": "json_object"}
        return None

    async def _complete(self, messages: List[Dict[str, str]]) -> str:
        model = await self._resolve_model()
        while True:
            body: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": self.settings.llm_temperature,
            }
            fmt = self._response_format()
            if fmt:
                body["response_format"] = fmt
            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.TimeoutException as e:
                raise LLMError(f"LLM request timed out after {self.settings.llm_timeout_seconds}s") from e
            except httpx.HTTPError as e:
                raise LLMError(f"Cannot reach LLM server at {self.base_url}: {e}") from e

            if resp.status_code in (400, 422) and fmt and self._mode_index < len(self._MODES) - 1:
                # Server doesn't support this response_format; degrade and retry.
                self._mode_index += 1
                log.warning("LLM server rejected response_format; falling back to %r mode", self.mode)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                raise LLMError(f"LLM server error {resp.status_code}: {resp.text[:300]}")
            if resp.status_code >= 400:
                raise LLMError(f"LLM request failed {resp.status_code}: {resp.text[:300]}", retryable=False)
            try:
                return resp.json()["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"Unexpected LLM response shape: {resp.text[:300]}") from e

    async def analyze(self, text: str) -> AnalysisResult:
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": text},
        ]
        last_error = ""
        for attempt in range(self.settings.llm_max_validation_retries + 1):
            content = await self._complete(messages)
            try:
                return AnalysisResult.model_validate(extract_json(content))
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = str(e)
                log.info("Validation failed (attempt %d): %s", attempt + 1, last_error.splitlines()[0])
                messages += [
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": "Your previous reply did not match the required schema:\n"
                        f"{last_error}\nReturn only the corrected JSON object.",
                    },
                ]
        raise LLMError(f"Model output failed validation: {last_error[:500]}")

    async def health(self) -> Dict[str, Any]:
        try:
            models = await self._list_models()
            return {"ok": True, "provider": self.name, "base_url": self.base_url,
                    "model": self._model or "auto", "available_models": models[:20], "mode": self.mode}
        except Exception as e:  # noqa: BLE001 - surface any failure in the health payload
            return {"ok": False, "provider": self.name, "base_url": self.base_url, "error": str(e)}


class MockAnalyzer:
    """Deterministic keyword-based triage. No model required."""

    name = "mock"

    _CATEGORY_RULES = [
        (Category.billing, r"invoice|bill|charg|refund|payment|price|subscription|plan|card|upgrade|downgrade"),
        (Category.account, r"password|login|log in|sign in|account|2fa|locked|email address|username"),
        (Category.technical, r"error|bug|crash|broken|not working|doesn't work|down|outage|slow|500|timeout|api"),
        (Category.sales, r"demo|quote|pricing for|enterprise|purchase|buy|licen[cs]e"),
        (Category.feedback, r"love|great|suggest|feature request|would be nice|feedback|hate"),
    ]
    _URGENT = r"outage|down for everyone|data loss|security|breach|hacked|production is down|asap|immediately|lawsuit"
    _HIGH = r"can't|cannot|unable|blocked|not working|broken|crash|locked out|charged twice|urgent"
    _MEDIUM = r"slow|error|wrong|incorrect|double charge|overcharg|today|deadline|soon"
    _NEG = r"angry|frustrat|terrible|awful|hate|unacceptable|disappoint|annoy|worst|ridiculous"
    _POS = r"thank|love|great|awesome|appreciate|excellent|happy"

    def __init__(self, delay_seconds: float = 0.0):
        self.delay_seconds = delay_seconds

    async def analyze(self, text: str) -> AnalysisResult:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        t = text.lower()
        category = next((c for c, rx in self._CATEGORY_RULES if re.search(rx, t)), Category.general)
        if re.search(self._URGENT, t):
            urgency = Urgency.urgent
        elif re.search(self._HIGH, t):
            urgency = Urgency.high
        elif re.search(self._MEDIUM, t):
            urgency = Urgency.medium
        else:
            urgency = Urgency.low
        if re.search(self._NEG, t):
            sentiment = Sentiment.negative
        elif re.search(self._POS, t):
            sentiment = Sentiment.positive
        else:
            sentiment = Sentiment.neutral
        first = re.split(r"(?<=[.!?])\s+", text.strip())[0]
        summary = first if len(first) <= 200 else first[:197] + "..."
        actions = {
            Category.billing: "Review the customer's billing record and respond about the requested change.",
            Category.account: "Verify the customer's identity and help them regain access to the account.",
            Category.technical: "Reproduce the reported problem and escalate to engineering if confirmed.",
            Category.sales: "Route to the sales team to follow up with the prospect.",
            Category.feedback: "Thank the customer and log the feedback for the product team.",
            Category.general: "Reply to the customer to clarify their request.",
        }
        return AnalysisResult(
            category=category, urgency=urgency, sentiment=sentiment,
            summary=summary, action_item=actions[category],
        )

    async def health(self) -> Dict[str, Any]:
        return {"ok": True, "provider": self.name}

    async def aclose(self) -> None:
        return None


def build_analyzer(settings: Settings) -> Analyzer:
    if settings.llm_provider == "mock":
        return MockAnalyzer()
    return OpenAICompatibleAnalyzer(settings)
