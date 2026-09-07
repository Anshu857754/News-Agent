# Startup Intelligence Newsletter

An AI-powered newsletter platform that finds what the world is searching for,
keeps only what matters to startups, researches it across news sources, and
writes a briefing with an LLM.

> **Status: Day 1 — foundation.** The architecture, API surface and OpenRouter
> abstraction are in place. The generation pipeline is **not built yet**, and
> `POST /api/newsletter/generate` says so rather than returning a fabricated
> newsletter.

---

## Table of contents

- [Vision](#vision)
- [Target pipeline](#target-pipeline)
- [Project structure](#project-structure)
- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [API endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Tests](#tests)
- [Roadmap](#roadmap)

---

## Vision

Give a founder or investor one thing to read each morning: not a firehose of
world news, but the handful of stories that actually move the startup world —
funding rounds, launches, acquisitions, market shifts — discovered
automatically rather than from a fixed keyword list, and written up so the
"why it matters" is already done.

---

## Target pipeline

```
Google Trends
  -> Trending Topic Discovery
  -> Startup Relevance Filtering
  -> Dynamic News Search
  -> Google News / Apify Collection
  -> Article Processing
  -> Deduplication
  -> Story Ranking
  -> OpenRouter LLM Analysis
  -> AI Generated Startup Newsletter
```

Each stage narrows the funnel, so the expensive work — article downloads and
LLM calls — only ever touches material that has already earned its place.

---

## Project structure

```
Startup Newsletter AI
│
├── backend
│   ├── app
│   │   ├── api
│   │   │   └── newsletter.py        # routes: /api/newsletter/*
│   │   │
│   │   ├── services
│   │   │   ├── openrouter_service.py  # every LLM call goes through here
│   │   │   └── newsletter_service.py  # pipeline orchestration
│   │   │
│   │   ├── models
│   │   │   └── schemas.py           # request/response contract
│   │   │
│   │   ├── core
│   │   │   └── config.py            # settings + logging
│   │   │
│   │   └── main.py                  # app entry point
│   │
│   ├── tests
│   │   └── test_newsletter.py       # offline test suite
│   │
│   └── requirements.txt
│
├── .env.example
├── .gitignore
└── README.md
```

---

## Quick start

```bash
# 1. Install dependencies
pip install -r backend/requirements.txt

# 2. Add your keys
cp .env.example .env        # Windows: copy .env.example .env
#    then edit .env and paste your OpenRouter key

# 3. Run the API (from the backend/ directory)
cd backend
uvicorn app.main:app --reload --port 8000
```

Then open <http://127.0.0.1:8000/docs> for interactive API docs.

```bash
# 4. Check it is alive
curl http://127.0.0.1:8000/api/newsletter/health
```

---

## Environment variables

All configuration comes from the environment or the project-root `.env`. No key
ever appears in code, and `.env` is git-ignored.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `OPENROUTER_API_KEY` | **yes** | — | Your OpenRouter key |
| `OPENROUTER_MODEL` | no | `google/gemini-2.5-flash` | Model used for every call |
| `APIFY_API_TOKEN` | no | — | Used by the collection stage |
| `OPENROUTER_BASE_URL` | no | `https://openrouter.ai/api/v1` | Override the endpoint |
| `OPENROUTER_TIMEOUT_SECONDS` | no | `30` | Per-request timeout |
| `OPENROUTER_MAX_RETRIES` | no | `2` | SDK-level retries |
| `OPENROUTER_TEMPERATURE` | no | `0.3` | Default sampling temperature |
| `LOG_LEVEL` | no | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

`APIFY_API_KEY` is accepted as an alias for `APIFY_API_TOKEN`, so an existing
`.env` using the older name keeps working.

---

## API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Service metadata |
| `GET` | `/health` | Liveness plus effective config (never the key) |
| `GET` | `/api/newsletter/health` | `{"status": "ok", "service": "newsletter"}` |
| `POST` | `/api/newsletter/generate` | Validates the request; returns `not_implemented` |
| `GET` | `/docs` | Interactive Swagger UI |

### Generating a newsletter

```bash
curl -X POST http://127.0.0.1:8000/api/newsletter/generate \
  -H "Content-Type: application/json" \
  -d '{
        "region": "Global",
        "category": "Startups",
        "time_range": "24h",
        "newsletter_type": "daily"
      }'
```

```json
{
  "message": "Newsletter generation pipeline foundation is ready",
  "status": "not_implemented",
  "request": {
    "region": "Global",
    "category": "Startups",
    "time_range": "24h",
    "newsletter_type": "daily"
  },
  "newsletter": null
}
```

**Request fields.** `time_range` accepts `24h`, `7d`, `30d`. `newsletter_type`
accepts `daily`, `weekly`, `monthly`. `region` and `category` are free-form
strings — they are editorial choices that will grow, so they are not frozen
into an enum. Every field has a default, so `{}` is a valid body.

---

## Architecture

Four layers, each with one job. The dependency direction only ever points
downward, so any layer can be tested without the one above it.

| Layer | Module | Responsibility |
|---|---|---|
| API | `app/api/newsletter.py` | Validate, delegate, serialise. No logic. |
| Service | `app/services/newsletter_service.py` | Pipeline orchestration |
| AI | `app/services/openrouter_service.py` | Every LLM call, in one place |
| Models | `app/models/schemas.py` | The contract between layers |
| Core | `app/core/config.py` | Settings and logging |

### The OpenRouter abstraction

Every future stage calls one of three methods rather than building its own
client. That is the point of the module: one place where the key is read, the
timeout is set, errors are classified and calls are logged.

```python
from app.services.openrouter_service import get_openrouter_service

service = get_openrouter_service()

text   = await service.generate_completion("Write a headline about ...")
data   = await service.generate_structured_output(system_prompt, user_prompt)
result = await service.analyze_content(article_body, "Score startup relevance 0-1")
```

- **The key comes from the environment**, never from code.
- **Timeouts and retries** are configured once, on the client.
- **Errors are typed**: `OpenRouterUnavailableError` (degrade) and
  `OpenRouterResponseError` (the model replied with something unusable). Every
  SDK exception is converted, because a caller's job is to fall back, not to
  interpret an `httpx` timeout.
- **JSON parsing is tolerant.** `response_format=json_object` is requested but
  never relied on — not every model honours it, so replies still go through an
  extractor that handles code fences and leading prose.

### Error handling and logging

`configure_logging()` runs once at startup; every module then uses a plain
`logging.getLogger(__name__)`. Logged events are deliberately few: application
startup and its effective config, each newsletter request, OpenRouter failures,
and unexpected errors.

A catch-all exception handler in `main.py` logs the traceback and returns a
generic `500`. Internals — and anything read from the environment — never reach
an HTTP response.

---

## Tests

```bash
cd backend
python -m pytest tests -q
```

Every test runs offline. No test makes a real OpenRouter call, so the suite
needs no API key and costs nothing. Coverage includes configuration, schema
validation, JSON extraction, error paths, all endpoints, and one regression
test asserting that no API key can appear in any response.

---

## Roadmap

Day 1 is the foundation only. Still to build:

- **Day 2** — Google Trends discovery and startup relevance filtering
- **Day 3** — dynamic news search and Google News / Apify collection
- **Day 4** — article processing, deduplication and story ranking
- **Day 5** — LLM analysis and the generated newsletter itself
- **Later** — persistence, scheduling and delivery
