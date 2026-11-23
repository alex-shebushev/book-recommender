# Book Recommender — app (stage 1)

Two things live here:

1. A minimal **agent loop** over the Anthropic Messages API, plus the first three
   tools (`find_similar`, `get_reviews_and_guides`, `get_book_details`) as
   standalone functions. The tools are **not wired into the agent yet**, so the
   chat still answers from the model's own knowledge.
2. A **FastAPI web API + React UI** (`api.py` + `static/`) for the catalog,
   book details, and a read-only bookshelf — see [Web app](#web-app-catalog--book-details--bookshelf) below.

See [`../DESIGN.md`](../DESIGN.md) for the full architecture and
[`../OPEN_QUESTIONS.md`](../OPEN_QUESTIONS.md) for open decisions.

## Stack

- **Python** (3.10+ — uses `X | Y` type syntax)
- **Anthropic SDK** (`anthropic`) — Messages API
- **Model:** `claude-sonnet-4-6` (chosen for testing; override via `AGENT_MODEL`)
- **boto3** — Bedrock Knowledge Base `Retrieve` (for `find_similar` and
  `get_reviews_and_guides`). The KB is a *managed* knowledge base, so requests
  use `managedSearchConfiguration`.
- **LangGraph** — only inside the `find_similar_by_bookshelf` tool (per-cluster
  fan-out); the agent loop itself is a bare request loop and needs none.
  `parse_bookshelf` will be the second graph-backed tool. Nodes call the raw
  `anthropic` SDK + boto3 — LangChain is installed transitively but not used.

## Files

| File | Role |
|------|------|
| `config.py` | Settings (model, max tokens, thinking, system prompt) from env/`.env` |
| `agent.py` | `Agent` class — conversation history + one request per turn |
| `main.py` | Entry point: hardcoded test questions, or an interactive REPL |
| `models.py` | Shared data types (`Book`, `SimilarBook`, `ReviewDoc`) |
| `isbn.py` | ISBN helpers — ISBN-13 as the shared form, ISBN-10 for KB lookups |
| `tools/find_similar.py` | Similar books from the Bedrock Knowledge Base, for a query text |
| `tools/find_similar_by_bookshelf.py` | Similar books for a shelf of `Book`s, minus an exclude list — LangGraph: prepare → cluster → retrieve (per cluster) → merge |
| `scripts/clustering_gate.py` | Q10 diagnostic: prints the LLM's clusters + each cluster's KB query for a shelf |
| `scripts/eval_bookshelf_query.py` | Eval: LLM-judged precision@8 + stability of the per-cluster KB query vs. LLM-blurb variants |
| `tests/` | Offline tests (fakes for the LLM, KB and Open Library) |
| `tools/get_reviews_and_guides.py` | The KB's own passages about one book, with a `source_id` to cite |
| `tools/get_book_details.py` | Book metadata from Open Library (no cache yet) |
| `api.py` | FastAPI web API (catalog / book details / shelf) + serves the SPA |
| `db.py` | Postgres connection pool for the web API |
| `schemas.py` | Pydantic response models for the web API |
| `static/` | No-build React SPA (ES-module import map + htm), styled per `../STYLE.md` |
| `.env.example` | Copy to `.env`, add your API key |

## Setup

```bash
cd "Book Recommender/app"
python -m venv .venv
.venv\Scripts\activate          # Windows (PowerShell: .venv\Scripts\Activate.ps1)
pip install -r requirements.txt
copy .env.example .env           # then edit .env and add ANTHROPIC_API_KEY
```

## Run

Hardcoded smoke test (3 questions, checks multi-turn memory):

```bash
python main.py
```

Interactive chat:

```bash
python main.py --interactive
```

Each tool can be run on its own (`find_similar` needs AWS credentials and
`BEDROCK_KB_ID`; `get_book_details` needs only internet access):

```bash
python -m tools.find_similar "a hopeful science-fiction novel about first contact"
```

```bash
python -m tools.get_book_details 9780441013593
```

To try `get_book_details` with any ISBNs (any form, several at once), use the
check script. With no arguments it runs three samples; add `--json` for raw
output or `--no-description` to skip the slower description calls:

```bash
python scripts/check_get_book_details.py 0-441-01359-7 9780061120084
```

To try `find_similar` with any query texts (quote each one), use its check
script. It reads `BEDROCK_KB_ID` from `.env`, so nothing needs exporting. With
no arguments it runs three samples; `--top-k`, `--min-score`, `--exclude` and
`--json` are available:

```bash
python scripts/check_find_similar.py "dark epic fantasy" "space opera" --top-k 3
```

To see what the KB holds about a book, use the `get_reviews_and_guides` check
script with any ISBNs (any form). With no arguments it runs three samples;
`--question` orders the passages by relevance to it, `--full` prints whole
passages, and `--top-k` and `--json` are available:

```bash
python scripts/check_get_reviews_and_guides.py 9780553283686 --full
```

`find_similar_by_bookshelf` takes the shelf as `Book`s (plus an optional
`exclude` list of books) and returns recommendations, each tagged with the
cluster it matches and whether it is *exploratory* (a minority taste). The web
app calls it from both "Get recommendation" buttons with catalog rows. Run on
its own, it takes ISBNs, resolves them through Open Library first, and prints
the clusters, each cluster's KB query, and the results. Needs `ANTHROPIC_API_KEY`, `BEDROCK_KB_ID`,
AWS credentials and internet; a 7-book shelf takes about 30 s this way:

```bash
python -m tools.find_similar_by_bookshelf 9780441013593 9780547928227 9780307588371
```

To judge the clustering alone (no KB call; it also prints each cluster's KB query), run the Q10 gate;
`--runs N` repeats the clustering to check it is stable:

```bash
python scripts/clustering_gate.py --runs 3
```

To measure recommendation quality, run the query eval: 6 test shelves, the
real KB, and an LLM judge that marks each of the top 8 hits relevant or not.
It prints precision@8 and run-to-run stability for the tool's query and two
LLM-blurb alternatives (needs the catalog DB too; about a minute):

```bash
python scripts/eval_bookshelf_query.py --out eval_results.json
```

Offline tests (no network, no keys):

```bash
python -m unittest discover -s tests -t . -v
```

## Web app (catalog · book details · bookshelf)

A FastAPI backend over Postgres that also serves a React single-page app. Stage-1
scope is **read-only**: no authentication — a single seeded demo user owns the
shelf (resolved behind one `current_user_id` dependency, so real auth slots in
later). Books, the demo user, and the demo shelf come from the seed in
[`../db/seed_data.json`](../db/seed_data.json).

**Prerequisites:** Docker (for Postgres) and the `app` venv with deps installed
(`pip install -r requirements.txt`). Node is **not** required — the UI uses a
no-build React (ES modules + import map from a CDN). Swap in a Vite build later
when Node is available; the components and CSS carry over.

```bash
cd "Book Recommender" && docker compose up -d
```

```bash
cd "Book Recommender/db" && python migrate.py && python seed.py
```

```bash
cd "Book Recommender/app" && uvicorn api:app --reload
```

Then open <http://localhost:8000/>. The API is under `/api` (e.g.
`/api/books`, `/api/books/{book_id}`, `/api/genres`, `/api/shelf`, `/api/health`).

Database connection resolves from `DATABASE_URL`, else the `POSTGRES_*` vars in
the repo-root `.env` (host `localhost`, port `5544` by default).

### Auth

Email + password login, bcrypt hashes in `user_details`, kept in a signed
HTTP-only session cookie. **No roles** — every signed-in user has the same
access; **sign-up is public**. Books + details are public; **shelves, ratings
and recommendations require sign-in**.

- Endpoints: `POST /api/auth/signup`, `POST /api/auth/login`,
  `POST /api/auth/logout`, `GET /api/auth/me`.
- Library: `GET /api/shelf` (every shelf with its books, Exclusion first).
  A user's library is their shelves; every user has a built-in **Exclusion**
  shelf (never used as taste, never recommended, can't be renamed or deleted).
- Shelves: `GET /api/shelves`, `POST /api/shelves`, `GET/PATCH/DELETE
  /api/shelves/{shelf_id}`, `PUT/DELETE /api/shelves/{shelf_id}/books/{book_id}`.
  Adding a book to Exclusion takes it off the user's other shelves and vice
  versa (enforced by a DB trigger).
- Ratings: `PUT /api/books/{book_id}/rating` with `{rating?, review?}` — only the
  fields sent change; independent of shelves.
- Recommendations (`find_similar_by_bookshelf`, ~7–30 s per call; need
  `ANTHROPIC_API_KEY`, `BEDROCK_KB_ID` and AWS credentials):
  `GET /api/shelves/{shelf_id}/recommendations` (seeded by one shelf; not the
  Exclusion shelf) and `GET /api/shelf/recommendations` (seeded by all normal
  shelves). Normal-shelf books are dropped; **Exclusion-shelf books are kept but
  tagged `excluded`** (T1) so the UI's "Hide books on your Exclusion shelf"
  checkbox can hide them or show them grayed. `GET /api/books/{id}/similar`
  carries the same `excluded` flag for the signed-in reader.
- Seeded base account: **`admin@bookshelf.com` / `4R5t6y7u_`** (`user_id = 1`,
  owns the demo shelves). A committed demo credential — not a real secret.
- `SESSION_SECRET` (see `.env.example`) signs the cookie; set a strong value in
  production.

## Docker

The app ships as a single container image (built from [`Dockerfile`](Dockerfile))
that serves the JSON API **and** the SPA on port `8000`. There is no Node build —
the SPA is static files baked into the image.

### Local dev (compose)

The repo-root `docker-compose.yml` is **local-dev only**: it runs Postgres and the
app together.

```bash
cd "Book Recommender"
cp .env.example .env            # POSTGRES_USER/PASSWORD/DB/PORT, APP_PORT
cp app/.env.example app/.env    # ANTHROPIC_API_KEY, SESSION_SECRET, BEDROCK_KB_ID, ...
docker compose up -d --build    # db + app  ->  http://localhost:8000
```

First run applies the DB init scripts in `db/init/`. To run uvicorn on the host
with `--reload` instead, start just the DB: `docker compose up -d db`.

### Deploy (standalone / EC2)

The container is designed to run on its own host, pointed at a **remote** database
(e.g. RDS) — compose is not used in production. Build and run the image directly:

```bash
docker build -t bookrec-app ./app

docker run -d --name bookrec-app -p 8000:8000 \
  -e DATABASE_URL="postgresql://USER:PASSWORD@DB_HOST:5432/bookrec" \
  -e SESSION_SECRET="$(openssl rand -hex 32)" \
  -e ANTHROPIC_API_KEY="sk-ant-..." \
  -e BEDROCK_KB_ID="XXXXXXXXXX" \
  bookrec-app
```

Required / recommended environment on the host:

| Variable | Why |
|----------|-----|
| `DATABASE_URL` | **Required.** Points at the remote DB — *not* `db:5432`. |
| `SESSION_SECRET` | **Required.** Strong random value; a restart invalidates sessions if it changes. |
| `ANTHROPIC_API_KEY` | Required for the assistant / recommendations. |
| `BEDROCK_KB_ID` | Required for `find_similar` (RAG). |
| AWS credentials | Prefer an **IAM instance role** on EC2 — boto3 picks it up automatically; do **not** bake keys into the image. Set `AWS_REGION` only to override the role's region. |

Notes:
- The DB must already have its schema + seed applied (run `db/migrate.py` and
  `db/seed.py` against the remote DB once).
- The EC2 security group must allow the app host → DB (5432) and inbound `8000`
  (or whatever you publish) from your clients / load balancer.
- Behind TLS (ALB/nginx), harden the session cookie: set `https_only=True` in
  `api.py` and update the CORS `allow_origins` if the SPA is served from another
  origin. Same-origin (this container serving the SPA) needs neither.
- Health: the image has a `HEALTHCHECK` hitting `/api/health` (which also checks
  DB connectivity) — visible via `docker ps` / usable as an ALB target check.

## Configuration

All optional, via `.env` or environment:

| Variable | Default | Meaning |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | **Required.** Your Anthropic key. |
| `AGENT_MODEL` | `claude-sonnet-4-6` | Model id. |
| `AGENT_MAX_TOKENS` | `4096` | Max output tokens per reply. |
| `AGENT_THINKING` | `false` | `true` enables adaptive thinking (slower, deeper). |
| `AGENT_SYSTEM_PROMPT` | built-in | Override the system prompt. |
| `BEDROCK_KB_ID` | — | Knowledge Base id. Required for `find_similar`. |
| `AWS_REGION` | AWS profile | Override the region from your AWS CLI profile. |
| `FIND_SIMILAR_TOP_K` | `5` | Not read by `find_similar` yet — pass `top_k=` instead. |
| `FIND_SIMILAR_MIN_SCORE` | `0.4` | Not read by `find_similar` yet — pass `min_score=` (its built-in default is `0`). |

## Notes

- The Messages API is stateless; `Agent` resends the full history each turn.
- No grounding yet — the model answers from its own knowledge, so it is told to
  flag anything that would need verification. Real grounding arrives with RAG.
