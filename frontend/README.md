# StartupPulse AI — frontend

The trend intelligence dashboard: a ranked view of trending search topics
scored for startup relevance.

Plain HTML, CSS and JavaScript. **No Node, no bundler, no `npm install`** —
there is nothing to build, so the files you edit are the files that ship.

```
frontend/
├── index.html     structure
├── styles.css     design system and layout
├── app.js         API client, components, state
└── config.js      where the backend lives  ← the file you edit
```

## Running it

The backend serves this folder, so one command runs the whole product:

```bash
cd backend
python -m uvicorn app.main:app --reload --port 8000
```

Then open <http://127.0.0.1:8000/trends>.

### Serving it separately

Useful if you want live-reload tooling on the frontend:

```bash
# terminal 1 - the API
cd backend && python -m uvicorn app.main:app --reload --port 8000

# terminal 2 - this folder
cd frontend && python -m http.server 5500
```

Then set `window.STARTUPPULSE_API_BASE = "http://127.0.0.1:8000"` in
`config.js` and open <http://127.0.0.1:5500>. The backend sends permissive
CORS headers, so nothing else needs changing.

> **Opening `index.html` directly from disk does not work.** A `file://` page
> cannot call an HTTP API. Use a local server, as above.

## What it talks to

| Call | When |
|---|---|
| `GET /api/trends/health` | On load — drives the status dot in the top bar |
| `GET /api/trends` | On load and on filter change — a free preview, no LLM |
| `POST /api/trends/discover` | Only when **Refresh Trends** is pressed |

**Opening the page costs nothing.** The model runs only on an explicit refresh,
so nobody spends credits by leaving a tab open.

## The page

- **Header** — title, region (India / Global), trend limit (10 / 20 / 30), and
  the **Refresh Trends** button.
- **Market signals** — total analyzed, startup relevant, average relevance,
  last updated.
- **Trend discovery** — a ranked list: rank, topic, category, `95 / 100`, the
  model's one-line reason, and the source.
- **States** — idle, skeleton loading, empty, and error with retry.

## Design

One accent colour on a dark ground, 1px hairline borders, flat fills, tabular
figures. No gradients, no glass, no glow.

| Role | Token |
|---|---|
| Background | `#0B0F14` |
| Cards | `#111827` |
| Border | `#1F2937` |
| Primary text | `#F9FAFB` |
| Secondary text | `#9CA3AF` |
| Accent | `#3B82F6` |

## Conventions worth keeping

- **No mock data.** Every figure on the page comes from the backend. Before a
  run the page shows an empty state, not a plausible-looking placeholder.
- **Errors are translated, never echoed.** `messageForStatus()` maps a status
  code to our own copy; the backend's detail goes to the console only.
- **Model output is escaped.** Topics and reasons pass through `escapeHtml()`
  before they reach the DOM.
- **Rules-only runs are labelled.** When the API returns `ai_used: false` the
  note under the heading says so instead of passing rule scores off as AI
  judgements.
- **Relevance never depends on colour alone** — a number, a bar, and a word
  (Critical / High / Moderate / Low).
- **State lives in one object.** Nothing writes to the DOM outside a render
  function.
