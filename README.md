# Startup Intelligence Newsletter

An AI-powered newsletter platform that finds what the world is searching for,
keeps only what matters to startups, researches it across news sources, and
writes a briefing with an LLM.

> **Status: Day 3 — personalisation.** Users, interest profiles, news
> ingestion, the intelligence engine, the recommendation engine and
> personalised newsletters are built and runnable, on top of Day 1's foundation
> and Day 2's trend discovery. News collection and
> newsletter generation are **not built yet**, so
> `POST /api/newsletter/generate` reports `not_implemented` rather than
> returning a fabricated newsletter.

---

## Table of contents

- [Vision](#vision)
- [Target pipeline](#target-pipeline)
- [Project structure](#project-structure)
- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [API endpoints](#api-endpoints)
- [Architecture](#architecture)
- [Day 2 — Trend Discovery Pipeline](#day-2--trend-discovery-pipeline)
- [Dashboard (frontend)](#dashboard-frontend)
- [Day 3 — Personalisation](#day-3--personalisation)
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
│   │   │   ├── newsletter.py        # routes: /api/newsletter/*
│   │   │   ├── trends.py            # routes: /api/trends/*
│   │   │   ├── users.py             # routes: /api/users/*
│   │   │   ├── feed.py              # routes: /api/users/{id}/feed
│   │   │   └── news.py              # routes: /api/news/*
│   │   │
│   │   ├── providers                # external sources live here only
│   │   │   ├── base.py              # BaseTrendProvider
│   │   │   ├── google_trends.py     # GoogleTrendsProvider (RSS)
│   │   │   ├── news_base.py         # BaseNewsProvider
│   │   │   └── google_news.py       # GoogleNewsProvider (RSS)
│   │   │
│   │   ├── services
│   │   │   ├── openrouter_service.py    # every LLM call goes through here
│   │   │   ├── newsletter_service.py    # writes an issue for one reader
│   │   │   ├── trend_service.py         # trend pipeline orchestration
│   │   │   ├── trend_filter.py          # rule-based pre-filter
│   │   │   ├── trend_relevance.py       # AI relevance classification
│   │   │   ├── user_service.py          # users, interests, behaviour
│   │   │   ├── ingestion_service.py     # collect, clean, dedupe, store
│   │   │   ├── intelligence_service.py  # topics, entities, importance, events
│   │   │   └── recommendation_service.py # scores and selects a feed
│   │   │
│   │   ├── models
│   │   │   ├── schemas.py           # request/response contract
│   │   │   └── db.py                # the 8 database tables
│   │   │
│   │   ├── core
│   │   │   ├── config.py            # settings + logging
│   │   │   ├── database.py          # engine, sessions, DATABASE_URL
│   │   │   └── cache.py             # in-memory TTL cache
│   │   │
│   │   └── main.py                  # app entry point
│   │
│   ├── tests
│   │   ├── test_newsletter.py       # Day 1 suite
│   │   ├── test_trends.py           # Day 2 backend suite
│   │   ├── test_frontend.py         # Day 2 dashboard suite
│   │   └── test_personalization.py  # Day 3 suite
│   │
│   └── requirements.txt
│
├── frontend                         # the dashboard (no build step)
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── config.js                    # where the backend lives
│   └── README.md
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
| `GET` | `/api/trends` | Raw normalised trends (see Day 2) |
| `POST` | `/api/trends/discover` | Full trend discovery pipeline (see Day 2) |
| `GET` | `/api/trends/health` | `{"status": "ok", "service": "trend-discovery"}` |
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

## Day 2 — Trend Discovery Pipeline

Finds what the world is searching for and keeps only what a startup newsletter
should care about. This stage is complete and runnable.

### Architecture

```
Google Trends
      ↓
Trend Collection        providers/google_trends.py  (RSS, cached)
      ↓
Normalization           every provider returns TrendItem
      ↓
Rule-Based Pre-Filter   services/trend_filter.py    (free; drops the noise)
      ↓
AI Relevance            services/trend_relevance.py (one batched LLM call)
      ↓
Threshold + Ranking     services/trend_service.py
      ↓
Relevant Startup Trends
```

The provider sits behind `BaseTrendProvider`, so the source can be replaced
without touching the service, the API or the tests.

### The pre-filter, and why it has three outcomes

Google Trends is mostly sport, television and celebrity news. Classifying all
of it with an LLM would spend most of the budget proving that a cricket match
is not a startup story, so cheap keyword rules go first:

| Verdict | Meaning | Goes to the LLM |
|---|---|---|
| `HIGH_PRIORITY` | A known startup/tech signal is present | yes |
| `POSSIBLE` | Nothing matched either way | yes |
| `LOW_PRIORITY` | A known noise signal, nothing to offset it | **no — discarded** |

The asymmetry is deliberate. An unknown company name looks exactly like
`POSSIBLE`, so absence of evidence never rejects a topic — only an explicit
noise match can.

### Cost control

- **Batched**: all surviving topics go up in one request (`TREND_AI_BATCH_SIZE`,
  default 25), not one call per topic.
- **Cached**: collected trends are held in memory for `TREND_CACHE_TTL_SECONDS`
  (default 15 minutes), keyed by region and limit. In-process, so it empties on
  restart — acceptable for data with that TTL, and it needs no Redis.
- **Degrades instead of failing**: if the model is unreachable, rate-limited or
  replies with unusable JSON, affected topics fall back to rule-based scores.
  The response's `ai_used` flag says which happened.

### API endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/trends` | Raw normalised trends. No LLM, so it costs nothing. |
| `POST` | `/api/trends/discover` | The full pipeline |
| `GET` | `/api/trends/health` | `{"status": "ok", "service": "trend-discovery"}` |

```bash
# Raw trends
curl "http://127.0.0.1:8000/api/trends?region=IN&limit=20"

# Full discovery pipeline
curl -X POST http://127.0.0.1:8000/api/trends/discover   -H "Content-Type: application/json"   -d '{"region": "IN", "limit": 20}'
```

```json
{
  "total_trends_collected": 10,
  "total_relevant_trends": 3,
  "region": "IN",
  "trends": [
    {
      "topic": "AI Agents",
      "category": "Artificial Intelligence",
      "relevance_score": 95,
      "reason": "Highly relevant to AI startups and technology innovation.",
      "region": "IN",
      "source": "google_trends",
      "trend_score": 0.25,
      "search_volume": 50000,
      "collected_at": "2026-09-08T05:50:00Z"
    }
  ],
  "generated_at": "2026-09-08T06:02:11Z",
  "ai_used": true
}
```

`region` is an ISO country code (`IN`, `US`, `GB`, …) or `GLOBAL`. An invalid
region is a `422`; the trend source being unreachable is a `503`. An empty
result is a `200` — "nothing today was startup-relevant" is a valid answer.

### What the data source does and does not provide

Trends come from Google's public daily trending-searches RSS feed. It needs no
key, no quota and no browser, and it is parsed with the standard library.

**Fields it does not supply are `null`, never invented.** `growth` is always
`null` for this provider. `search_volume` comes from the feed's approximate
traffic figure when present, and `trend_score` is derived from it — `0.0` when
the feed gives nothing, which is what "the source told us nothing" should look
like.

Google Trends has **no worldwide edition**. `GLOBAL` is composed by merging the
country feeds in `TREND_GLOBAL_REGIONS` and deduplicating by topic; every
returned item still reports the country it was actually found in.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required for AI classification |
| `OPENROUTER_MODEL` | `google/gemini-2.5-flash` | Model used |
| `TREND_RELEVANCE_THRESHOLD` | `70` | Minimum score to be returned |
| `TREND_DEFAULT_REGION` | `IN` | Used when a request omits it |
| `TREND_DEFAULT_LIMIT` | `20` | Used when a request omits it |
| `TREND_GLOBAL_REGIONS` | `US,GB,IN` | What `GLOBAL` is composed from |
| `TREND_CACHE_TTL_SECONDS` | `900` | `0` disables caching |
| `TREND_PROVIDER_TIMEOUT_SECONDS` | `15` | Per-request timeout |
| `TREND_AI_BATCH_SIZE` | `25` | Topics per LLM request |

Without `OPENROUTER_API_KEY` the pipeline still runs: it scores by rules alone
and returns `ai_used: false`.

---

## Dashboard (frontend)

**StartupPulse AI** — a trend intelligence dashboard served by the same FastAPI
process. Open <http://127.0.0.1:8000/trends> once the app is running.

There is **no Node, no bundler and no `npm install`**. The page is plain
HTML/CSS/JS in `frontend/`, which the backend mounts, so
`pip install -r backend/requirements.txt` remains the entire setup and one
command runs the whole product. The files sit outside the backend package, so
the frontend can also be hosted on its own — see [frontend/README.md](frontend/README.md).

### What is on the page

- **Header** — title, subtitle, region selector (India / Global), trend limit
  (10 / 20 / 30) and a **Refresh Trends** button that calls
  `POST /api/trends/discover`.
- **Market signals** — four compact metrics: total analyzed, startup relevant,
  average relevance, last updated (a live relative timestamp).
- **Trend discovery** — a ranked list: rank, topic, category, `95 / 100`,
  the model's one-line reason, and the source.
- **States** — idle, skeleton loading (*"Analyzing market signals…"*), empty,
  and error with a retry button.

### Design

One accent colour on a dark neutral ground, 1px hairline borders, flat fills
and tabular figures. No gradients, no glass, no glow. Rows sit in a single
bordered container divided by hairlines rather than nested cards.

| Role | Token |
|---|---|
| Background | `#0B0F14` |
| Cards | `#111827` |
| Border | `#1F2937` |
| Primary text | `#F9FAFB` |
| Secondary text | `#9CA3AF` |
| Accent | `#3B82F6` |

### Behaviour worth knowing

- **Discovery is never automatic.** Opening the page costs nothing: it runs a
  health check and a free `GET /api/trends` preview to fill the "collected"
  metric. The LLM only runs when you press **Refresh Trends**.
- **Average relevance is computed in the browser** from the returned scores —
  the API has no such field.
- **Rules-only runs are labelled.** If the model was unreachable the note under
  the heading reads *"scored by rules (AI unavailable)"* rather than passing
  rule scores off as AI judgements.
- **Errors never leak.** The UI maps status codes to its own copy; backend
  detail goes to the browser console only.
- **No mock data.** Every figure comes from the backend; before a run the page
  shows an empty state rather than a plausible placeholder.
- **Accessibility** — semantic landmarks, labelled selects, visible focus
  rings, an `aria-live` results region, and relevance conveyed by a number and
  a word (Critical / High / Moderate / Low), never by colour alone.

### Route change

The dashboard now owns `/`, so the machine-readable service index moved from
`/` to **`/api`**. Nothing else changed; `/health`, `/docs` and every
`/api/...` endpoint are untouched.


---

## Day 3 — Personalisation

The architecture the rest of the project is being built toward:

```
USER (login / preferences)
  -> USER INTEREST PROFILE      weighted 0-100 per topic
  -> Google Trends + News Sources
  -> INGESTION PIPELINE         collect, normalize, deduplicate, validate URLs
  -> NEWS INTELLIGENCE ENGINE   topics, entities, importance, events
  -> DATABASE                   8 tables
  -> RECOMMENDATION ENGINE      interest, importance, recency, diversity, behaviour
  -> PERSONALISED FEED          one per user
  -> NEWSLETTER                 OpenRouter, written for that reader
```

### Database

`DATABASE_URL` decides everything. It defaults to a SQLite file so the project
runs with nothing installed, and points at PostgreSQL in any real deployment —
no query in the codebase is dialect-specific.

```bash
DATABASE_URL=sqlite:///./data/startuppulse.db          # default
DATABASE_URL=postgresql+psycopg://user:pass@host/db    # production
```

Eight tables: `users`, `topics`, `articles`, `article_topics`, `events`,
`user_interests`, `recommendations`, `user_behavior`. Created on startup;
the moment the schema changes under real data this becomes Alembic.

Two shapes are worth knowing:

- **Topics are rows, not strings.** `article_topics` carries a confidence, so
  "how strongly is this article about AI?" is answerable. A comma-separated
  column could not answer it.
- **Stated and revealed preference are separate.** `user_interests` is what
  someone says they care about; `user_behavior` is what they actually opened.
  The recommender reads both, because the disagreement is the signal.

### The pipeline

| Stage | Module | What it does |
|---|---|---|
| Ingestion | `services/ingestion_service.py` | Collect → normalize → validate → dedupe → store |
| Intelligence | `services/intelligence_service.py` | Topics, entities, importance, event clustering |
| Recommendation | `services/recommendation_service.py` | Scores and selects one user's feed |
| Newsletter | `services/newsletter_service.py` | Writes the issue for that reader |

**Deduplication runs twice, for two different duplicates.** The canonical URL
catches the same link (tracking parameters stripped first, or one article looks
like five). Headline overlap catches the same story re-headlined by another
outlet — with money normalised first, so `$10M` and `$10 million` match while
`$5M` and `$50M` stay apart.

**Event clustering is not the LLM's job.** Grouping N articles is an O(N²)
comparison that rules do just as well and for nothing. It is what stops a feed
showing one funding round five times.

**Selection is greedy, not a sort.** The diversity penalty depends on what has
already been picked, so it can only be applied while selecting — sorting once
and slicing returns five funding stories in a row.

**Everything degrades.** If OpenRouter is unreachable, rule-based tagging and
scoring run instead and the newsletter is assembled from stored fields. The
response says which happened rather than passing rules off as AI judgement.

### API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/users` | Create a user, optionally with interests |
| `GET` | `/api/users` | List users |
| `GET` | `/api/users/{id}` | One user and their profile |
| `PUT` | `/api/users/{id}/interests` | Replace the interest profile |
| `POST` | `/api/users/{id}/behavior` | Record a view / open / dismiss |
| `GET` | `/api/users/{id}/feed` | The personalised feed |
| `POST` | `/api/news/ingest` | Run ingestion, then the intelligence engine |
| `POST` | `/api/newsletter/generate` | Write an issue for a user |

### Walk-through

```bash
# 1. A reader and what they care about
curl -X POST http://127.0.0.1:8000/api/users   -H "Content-Type: application/json"   -d '{"email":"founder@example.com","display_name":"Anshu","region":"IN",
       "interests":[{"topic":"AI","weight":80},{"topic":"Funding","weight":90},
                    {"topic":"Fintech","weight":70},{"topic":"Cricket","weight":30}]}'

# 2. Collect news for their interests, and analyse it
curl -X POST http://127.0.0.1:8000/api/news/ingest   -H "Content-Type: application/json"   -d '{"user_id":1,"region":"IN","limit_per_query":6}'

# 3. Their feed
curl "http://127.0.0.1:8000/api/users/1/feed?limit=6"

# 4. Their newsletter
curl -X POST http://127.0.0.1:8000/api/newsletter/generate   -H "Content-Type: application/json" -d '{"user_id":1,"limit":4}'
```

Every feed item carries the reason it was chosen and the scores behind it:

```json
{
  "title": "Positron Valued at $5 Billion as Demand for AI Chips Surges",
  "topics": ["ai", "funding"],
  "score": 88.3,
  "reason": "Matches your interest in funding (90/100); widely significant story.",
  "breakdown": {"interest": 90, "importance": 90, "recency": 83,
                "behavior": 0, "diversity_penalty": 0}
}
```

The breakdown is stored, not just returned — without it "why am I seeing this?"
is unanswerable and the weights cannot be tuned against real output.

### Newsletter behaviour

`POST /api/newsletter/generate` **needs a `user_id`**. An issue is written for
one reader's interest profile, so without a user it reports `not_implemented`
rather than inventing a generic issue.

The model writes prose, never facts: every headline, link and source in the
output comes from the database. It is given the stories and asked to introduce
and summarise them — it is never asked what the news is.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/startuppulse.db` | SQLite or PostgreSQL |
| `REC_WEIGHT_INTEREST` | `0.45` | Weight of interest matching |
| `REC_WEIGHT_IMPORTANCE` | `0.30` | Weight of story importance |
| `REC_WEIGHT_RECENCY` | `0.25` | Weight of freshness |
| `REC_BEHAVIOR_INFLUENCE` | `20` | How far behaviour moves a score |
| `REC_DIVERSITY_PENALTY` | `12` | Deducted per repeated topic |
| `REC_BASELINE_INTEREST` | `12` | Score for an unmatched topic |
| `REC_RECENCY_HALFLIFE_HOURS` | `24` | Freshness half-life |
| `FEED_DEFAULT_LIMIT` | `10` | Feed size |
| `FEED_MAX_AGE_HOURS` | `72` | How far back candidates go |

### Not built yet

Authentication (users are created without passwords), email delivery,
scheduling, and a frontend for the feed — the dashboard still covers trend
discovery only.

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

- ~~**Day 2** — Google Trends discovery and startup relevance filtering~~ **(done)**
- ~~**Day 3** — users, ingestion, intelligence, recommendations, personalised newsletters~~ **(done)**
- **Day 3** — dynamic news search and Google News / Apify collection
- **Day 4** — article processing, deduplication and story ranking
- **Day 5** — LLM analysis and the generated newsletter itself
- **Later** — persistence, scheduling and delivery
