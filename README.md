# ⚡ Triaxa

> **Triaxa** — AI ticket triage. *Sort every support message by category, urgency and next step, automatically.*

[![CI](https://github.com/valentinakiyungi/async-agent-pipeline/actions/workflows/ci.yml/badge.svg)](https://github.com/valentinakiyungi/async-agent-pipeline/actions/workflows/ci.yml)
[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688.svg)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

An asynchronous ticket-triage service. Send it free-text support messages; it uses an LLM to return a
validated, structured record — **category, urgency, sentiment, summary and next action** — either
instantly or through a persistent background job queue with retries and webhooks. Works with local
models (LM Studio, Ollama, vLLM, llama.cpp) or any OpenAI-compatible API, and ships with a web dashboard.

---

## ✨ Features

* **Web dashboard** at `/` — submit single tickets or batches, watch jobs move through the queue, filter by status/category/urgency, inspect, re-run and delete jobs.
* **Two ways to analyze**
  * `POST /analyze` — synchronous, returns the result directly.
  * `POST /jobs` / `POST /jobs/batch` — queued, persisted in SQLite, processed by concurrent async workers.
* **Guaranteed structure** — requests JSON-schema constrained output, validates it with Pydantic, and when a model returns something invalid it feeds the validation errors back and asks again. Falls back automatically for servers that don't support `json_schema` response formats.
* **Resilient pipeline** — automatic retries with exponential backoff for transient failures (model loading, timeouts, 5xx), fast failure for permanent ones, and jobs interrupted by a restart are resumed on startup.
* **Webhooks** — pass `callback_url` and the finished job is POSTed to you.
* **Truly async** — non-blocking HTTP to the model and the database, so one slow generation never stalls the API.
* **Configurable** via environment variables or `.env`; optional API-key auth; Docker image; test suite with CI.
* **Mock mode** — `LLM_PROVIDER=mock` runs everything with a deterministic keyword classifier, no model required.

---

## 🚀 Quick start

### 1. Install

```bash
git clone https://github.com/valentinakiyungi/async-agent-pipeline.git
cd async-agent-pipeline
python3 -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Start a model server

**LM Studio:** load a model (Llama 3, Mistral, Qwen…), open the *Developer* tab and start the server
(default `http://127.0.0.1:1234/v1`). The defaults in `.env` already point there.

**Ollama:** `ollama pull llama3.1`, then in `.env` set
`LLM_BASE_URL=http://127.0.0.1:11434/v1` and `LLM_MODEL=llama3.1`.

**OpenAI:** set `LLM_BASE_URL=https://api.openai.com/v1`, `LLM_API_KEY=sk-…`, `LLM_MODEL=gpt-4o-mini`.

**No model yet?** Set `LLM_PROVIDER=mock` to try everything end to end.

### 3. Run

```bash
uvicorn main:app --reload --port 8000
```

Open **http://127.0.0.1:8000** for the dashboard, or **http://127.0.0.1:8000/docs** for interactive API docs.
The header dot turns green once the model server is reachable.

### 4. Try the client

```bash
python test_client.py                                   # queues 5 sample tickets and prints results
python test_client.py analyze "I was charged twice this month"
python test_client.py submit "Login page shows a 500 error" --wait
python test_client.py batch tickets.txt --wait          # one ticket per line
python test_client.py list --status failed
python test_client.py health
```

---

## 🔌 API

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `GET /health/ready` | Readiness: is the model server reachable? (503 if not) |
| `POST /analyze` | Analyze one text synchronously |
| `POST /jobs` | Queue one text → `202` with the job |
| `POST /jobs/batch` | Queue many texts |
| `GET /jobs` | List jobs; filter with `status`, `category`, `urgency`; paginate with `limit`, `offset` |
| `GET /jobs/{id}` | Get one job and its result |
| `POST /jobs/{id}/retry` | Re-run a completed or failed job |
| `DELETE /jobs/{id}` | Delete a job |
| `GET /stats` | Counts by status, category and urgency; queue depth; average processing time |

**Example**

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H 'Content-Type: application/json' \
  -d '{"text": "Our dashboard is down and every API call returns 500!",
       "metadata": {"ticket_id": "T-1042"},
       "callback_url": "https://example.com/hooks/triage"}'
```

A finished job:

```json
{
  "id": "5f0c…",
  "status": "completed",
  "text": "Our dashboard is down and every API call returns 500!",
  "result": {
    "category": "Technical",
    "urgency": "Urgent",
    "sentiment": "Negative",
    "summary": "The customer's dashboard is down and all API calls return HTTP 500 errors.",
    "action_item": "Escalate to on-call engineering and check API service health."
  },
  "attempts": 1,
  "metadata": {"ticket_id": "T-1042"},
  "duration_ms": 2140,
  "created_at": "…", "completed_at": "…"
}
```

Values: **category** `Billing | Technical | Account | Sales | Feedback | General` ·
**urgency** `Low | Medium | High | Urgent` · **sentiment** `Positive | Neutral | Negative` ·
**status** `queued | processing | completed | failed`.

---

## ⚙️ Configuration

All settings are environment variables (or lines in `.env`). See [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `openai` | `openai` (any compatible server) or `mock` |
| `LLM_BASE_URL` | `http://127.0.0.1:1234/v1` | Model server URL (`OPENAI_API_BASE` also accepted) |
| `LLM_API_KEY` | `not-needed` | API key for the model server (`OPENAI_API_KEY` also accepted) |
| `LLM_MODEL` | `auto` | Model name; `auto` uses the first model the server reports |
| `LLM_TEMPERATURE` | `0.0` | Sampling temperature |
| `LLM_TIMEOUT_SECONDS` | `120` | Per-request timeout |
| `LLM_MAX_VALIDATION_RETRIES` | `2` | Re-prompts when output fails schema validation |
| `WORKER_CONCURRENCY` | `2` | Jobs processed in parallel |
| `MAX_JOB_ATTEMPTS` | `3` | Attempts before a job is marked failed |
| `RETRY_BACKOFF_SECONDS` | `2` | Base delay, doubled after each failed attempt |
| `MAX_BATCH_SIZE` | `100` | Max items per `/jobs/batch` request |
| `MAX_TEXT_LENGTH` | `20000` | Max characters per text |
| `DATABASE_PATH` | `data/triaxa.db` | SQLite file |
| `API_KEY` | *(unset)* | If set, every API call needs `X-API-Key` |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `LOG_LEVEL` | `INFO` | Logging level |

> **Tip:** local servers usually process one generation at a time. Keep `WORKER_CONCURRENCY` at 1–2 for
> LM Studio; raise it for vLLM or hosted APIs.

---

## 🐳 Docker

```bash
docker compose up --build
```

The container reaches LM Studio/Ollama on your host via `host.docker.internal`; job history is kept in
the `triaxa-data` volume.

---

## 🧪 Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite runs without a model: it uses the mock analyzer for the API and a fake OpenAI-compatible
server for the LLM client (schema validation retries, response-format fallback, error classification,
model auto-detection, webhooks, crash recovery).

---

## 📂 Project structure

```
app/
  main.py        FastAPI app, routes, lifecycle
  pipeline.py    async job queue, workers, retries, webhooks
  llm.py         OpenAI-compatible analyzer with validation/retry loop; mock analyzer
  store.py       SQLite job persistence
  schemas.py     Pydantic models for API and LLM output
  config.py      environment-based settings
  static/        web dashboard
main.py          entry point (`uvicorn main:app`)
test_client.py   command-line client
tests/           pytest suite
```
