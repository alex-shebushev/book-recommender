# Book Recommender — Concept & Architecture

> Capstone project for the AI course. This document captures the concept, architecture,
> components, and build plan. Open questions and deferred items live in
> [`OPEN_QUESTIONS.md`](OPEN_QUESTIONS.md).

---

## 1. Problem & user

**User:** a reader who wants their next book and doesn't know what to pick.

**Problem:** generic "bestseller" lists ignore personal taste, hard constraints
(length, content to avoid), and what the reader already owns/read.

**Result the app delivers:** a short, **grounded, cited** list of book
recommendations with visible confidence and a clear statement of what it could
*not* answer — presented for a human (the reader) to review, accept, or reject.

**Primary goal of the project:** gain hands-on experience with a modern AI stack
(agent + tools, RAG, vision, AWS). It is a non-commercial experiment, not a
production product.

---

## 2. Core concept

The reader submits their taste (free text + optionally a **bookshelf photo**). An
**agent** researches a catalog and a **knowledge base**, then produces a cited
shortlist. Validation guarantees every recommended book is real and respects the
reader's hard constraints; weak matches are flagged.

Two complementary flows over the **same KB + DB**:

| Flow | Direction | Where it runs |
|------|-----------|---------------|
| Interactive recommendations | user → books | code (agent loop) |
| Interest/marketing campaign | book → users | **n8n** pipeline |

---

## 3. Rubric coverage

| Requirement | How it's met |
|-------------|--------------|
| **Own frontend** | Custom React app: submit taste + photo, view result cards. Not a chat wrapper. |
| **AI workflow** | Structured taste extraction; agent selects & calls tools; **image analysis** via `parse_bookshelf`. |
| **Grounded answers (RAG)** | Bedrock Knowledge Base `Retrieve`; every "why" carries a `source_id`; missing info surfaced. |
| **Validation & evaluation** | Input validation, output validation (books exist / constraints met / cited), confidence flags, graceful failure. |
| **AWS deployment** | EC2 (containers), Postgres, Bedrock KB; IAM roles, restricted network, secrets, CloudWatch logs. |
| **Technology choices** | Python/FastAPI, React, Claude (agent+vision), Bedrock embeddings — justified below. |
| **n8n (optional)** | Independent outbound "find interested readers" pipeline. |

---

## 4. Architecture

```
                         ┌───────────────────────────┐
   Reader ──────────────▶│   React frontend (UI)      │
                         └──────────────┬────────────┘
                                        │ REST (file passed in memory)
                                        ▼
                         ┌───────────────────────────┐
                         │  FastAPI backend           │
                         │  ┌─────────────────────┐   │
                         │  │  AGENT LOOP (Claude) │   │
                         │  │  + tool calling      │   │
                         │  └─────────┬───────────┘   │
                         │  post-agent VALIDATION step │
                         └─────┬───────────┬──────────┘
                               │           │
        ┌──────────────────────┘           └───────────────────────┐
        ▼                                                           ▼
  ┌───────────┐   ┌──────────────┐   ┌───────────────┐      ┌──────────────┐
  │ Bedrock   │   │ Postgres DB  │   │ Open Library  │      │ parse_bookshelf│
  │ Knowledge │   │ (catalog +   │   │ API (metadata)│      │ LangGraph +    │
  │ Base      │   │ users/lists) │   │               │      │ vision         │
  └───────────┘   └──────────────┘   └───────────────┘      └──────────────┘

  ─ ─ ─ ─ ─ ─ independent n8n pipeline (book → interested users → SES email) ─ ─ ─
```

**Orchestration decision:** the top level is a **plain agent loop with tools**
(no outer LangGraph). LangGraph is used inside **two tools** — `parse_bookshelf`
(vision + retry/disambiguation) and `find_similar_by_bookshelf` (shelf clustering +
per-cluster fan-out) — never at the top level.
Output validation lives in an explicit **post-agent code step** (not a graph node).

---

## 5. Agent tools

| Tool | Signature (sketch) | Backend | Uses AI? |
|------|--------------------|---------|----------|
| `parse_bookshelf` | `(image_bytes, media_type) -> {matches, notes}` | **LangGraph + vision** (ShelfPage feature, not an agent tool) — *implemented* | yes |
| `find_similar` | `(query, *, exclude_isbns, top_k, min_score) -> list[SimilarBook]` | **Bedrock KB `Retrieve`** (boto3) — *implemented* | yes (retrieval) |
| `find_similar_by_bookshelf` | `(books: list[Book], *, top_k=6, exclude: list[Book], min_score) -> list[SimilarBook]`, each hit tagged `cluster` / `exploratory` (`find_similar_by_bookshelf_detailed` adds clusters, skipped books, notes) | **LangGraph** over `find_similar`; the caller supplies the books (catalog rows, `get_book_details`, `parse_bookshelf`) — *implemented*, behind both bookshelf "Get recommendation" buttons | yes (cluster node) |
| `get_reviews_and_guides` | `(isbn, *, question, top_k) -> list[ReviewDoc]` (empty if the KB has nothing) | **Bedrock KB `Retrieve`** (same KB), pinned to the book by an ISBN metadata filter — *implemented* | yes (retrieval) |
| `get_book_details` | `(isbn) -> Book` (`None` if not found) | Open Library, called directly (Postgres cache later) — *implemented* | no |
| `search_catalog` | `(filters) -> list[Book]` | Postgres | no |
| `check_availability` | `(isbn) -> Availability` | async HTTP fan-out | no |
| `check_content_warnings` | `(book_id) -> list[str]` | DB / metadata | no |
| `get_user_library` | `(user_id) -> {shelves: [{name, is_exclusion, books}], ratings}` — Exclusion books are exclusions, never taste | Postgres | no |

**Design rules:**
- Tools that touch AI: `parse_bookshelf` (vision), the two KB retrieval tools, and
  `find_similar_by_bookshelf` (its `cluster` node). The rest are deterministic
  data access, and the **agent remains the single top-level reasoning layer**.
- `find_similar` is a **pure-retrieval primitive** (query text in, books out);
  `find_similar_by_bookshelf` is the graph that wraps it for shelf / multi-seed input.
- The tool layer never invents a book: every recommended book must resolve to a
  real catalog / Open Library entry.
- `find_similar` and `get_reviews_and_guides` share **one** Bedrock KB, queried
  differently (seed-book themes vs. a book's reviews).

### `parse_bookshelf` pipeline (LangGraph) — as built

A standalone **ShelfPage** feature (not an agent tool): upload a shelf photo →
review the matched books → confirm to add them to that shelf. The image is held
**in memory only** (base64 into the vision call) and never stored (D1).

```
image ─▶ [extract]  Claude vision: spine → {title, title_en, authors, language}
      ─▶ [match]    per book, DB cascade by language (detected → en → any),
                    fuzzy title + author → best catalog book, or dropped
      ─▶ matches (unmatched spines omitted) + notes ("read N, matched M")
```

- **Match against the catalog, not the KB.** The catalog is a superset of the KB
  books (T2) with a real `language` column, so the cascade and an exact-ish title
  match are precise there; the KB (semantic) would add nothing and can't filter by
  language. **Cross-language** spines are handled at the vision step — the model
  emits a canonical/English `title_en` to match on, with authors (language-stable)
  as the anchor.
- **Unmatched spines are omitted** (decided in this build; the original T3 note
  said "show them" — reversed), with a count note for feedback. The language
  cascade is an edition preference for the future; the catalog is ~all English
  today, so it rarely differs.
- Confirmed books are added via `POST /api/shelves/{id}/books/bulk`. Files:
  `app/tools/parse_bookshelf.py`, endpoints in `api.py`, UI `PhotoImport` in
  `pages.js`; offline tests `tests/test_parse_bookshelf.py`.

### `find_similar_by_bookshelf` pipeline (LangGraph) — shelf / multi-seed similarity

Recommends books similar to a **set** of seed books (a whole shelf, or one). It is
a **code-orchestrated LangGraph tool** (the 2nd graph-backed tool): the graph owns
the control flow; the LLM is used **only** in the `cluster` node (an LLM `blurb`
node was measured and replaced by a plain query — see "As built"). Nodes **reuse**
existing tools — they do not reimplement them.

```
isbns ─▶ [resolve]   reuse get_book_details per isbn -> {isbn,title,desc,genre?}
      ─▶ [cluster]   LLM: genre-first; infer genre only where missing; then group
                     uniformly into coherent buckets (compound genres handled)
      ─▶ [query]     per cluster, no LLM: "Genres: <genres>." + the books' own
                     descriptions (embedded; mirrors how KB records read)
      ─▶ [retrieve]  fan-out: reuse find_similar() once PER cluster (= true OR,
                     not a single centroid query); server-side exclude shelf ISBNs
      ─▶ [merge]     slot allocation: FLOOR per cluster + proportional remainder
                     (majority genre dominates, minority still shown); dedup across
                     clusters (best score); optional "exploratory" flag on minority
      ─▶ list[SimilarBook]
```

**Why these choices:**
- **Per-cluster queries (true OR), not one combined query.** A single centroid of a
  mixed shelf matches nothing well; per-cluster keeps "mostly fantasy + some X you'd
  also enjoy" alive.
- **Floor + proportional slots** encode "majority taste dominates, minority still shown".
- **Query = genres + the books' own descriptions**, not an LLM blurb and not a bare
  label. KB records read "<Title>. by <Author>. Genres: …", so this embeds next to
  the right books. The original LLM blurb ("themes/tone") was measured and dropped —
  see "As built".
- **Genre-first, LLM gap-fill:** use the genre field where present, LLM infers only the
  missing ones, then group uniformly (avoids inconsistent buckets).
- **LangGraph, not LangChain:** plain-Python nodes calling the raw `anthropic` SDK +
  `boto3`; LangChain would re-wrap primitives we already have. The graph earns its place
  via dynamic per-cluster parallel fan-out + observability (and the learning goal), not
  heavy branching — a plain loop could also do it.

**Caveats carried in:** server-side ISBN exclusion is edition-blind (Q9); `min_score`
needs calibration; descriptions ideally come from `parse_bookshelf`/catalog so `resolve`
can short-circuit. **Gating step:** validate the LLM on `cluster` + `blurb` (the
clustering test, Q10) before building — everything downstream assumes those nodes work.

**As built** (`app/tools/find_similar_by_bookshelf.py`):
- Input is **books, not ISBNs**: the caller passes the shelf as `Book`s plus an
  `exclude` list of books that must not come back. So `resolve` became `prepare`
  (validate + dedupe, no lookups) — the "short-circuit" above, made the only path.
  The web API builds the `Book`s from catalog rows, including the clean `genres`
  column (new `Book.genres` field), which the cluster prompt shows first.
- Graph: `prepare → cluster → retrieve (Send, per cluster) → merge`.
- **Per-cluster KB query** (replaced the LLM blurb): `"Genres: <the cluster's
  genres>."` + the member books' descriptions (≤400 chars each, ≤4,000 total; a
  book without one contributes "<title> by <authors>"). Deterministic, readable,
  and returned per cluster as `query` (the UI shows it as the chip's tooltip).
  Measured with `app/scripts/eval_bookshelf_query.py` — 6 shelves × 2 runs, an LLM
  judge labels each of the top 8 hits relevant or not:

  | query variant | precision@8 | run-to-run overlap |
  |---|---|---|
  | LLM blurb (original) | 0.38–0.41 | 0.22–0.31 |
  | LLM blurb told to keep genres, concrete elements, audience | 0.69–0.72 | 0.52–0.55 |
  | **genres + descriptions (as built)** | **0.71–0.72** | **1.00** |

  The abstract blurb was the main cause of off-genre picks (*Silver Linings
  Playbook* for a Hobbit + Game of Thrones shelf). The concrete LLM blurb tied on
  precision but is unstable and costs a call per cluster. Because a description
  query pulls a shelf book's other editions to the top, each cluster fetches
  2× the slot budget so the title drops in `merge` don't leave slots empty.
- The Q10 gate passed only after adding a sci-fi/fantasy separation rule to the
  cluster prompt (Dune drifted into fantasy without it). This SDK build accepts
  no `temperature`, so stability is prompt-driven. Labels still vary in wording
  between runs; membership is stable, and nothing keys off a label.
- Slots: 1 floor slot per cluster, remainder by largest remainder on cluster
  size (integer arithmetic — with floats, equal remainders differed in the last
  bit and a 1-book cluster took a 4-book cluster's slot); slots a cluster can't
  fill are backfilled by the best remaining hits. A cluster smaller than the
  largest one is `exploratory`.
- Exclusion (shelf + `exclude` books) is client-side through `find_similar` (all
  ISBN forms), plus a title check in `merge` that drops other editions (Q9).
- **Soft exclusion (T1):** `soft_exclude` books (the reader's Exclusion shelf)
  are *kept and tagged* `excluded=True` rather than dropped, so the UI can hide
  them (default) or show them grayed. They go to `find_similar` as `flag_isbns`,
  not `exclude_isbns`. `find_similar` itself gained `flag_isbns` for the same
  purpose on the book-page "Find similar". The API requests 10 (not 8) so hiding
  excluded hits usually still leaves several.
- Degrades: books without a valid ISBN → `skipped`; unusable clustering → one
  whole-shelf cluster (the parser also tolerates prose around the JSON); a failed
  search empties only its cluster. Raises only if every cluster's search failed.
- A single-book shelf makes no LLM call at all. From catalog rows a run takes
  3–5 s (one LLM call + parallel KB searches); resolving through Open Library
  first, with LLM blurbs, took ~30 s.
- **Web API + UI:** `GET /api/shelves/{id}/recommendations` (seeds = that
  shelf; refused for the Exclusion shelf) and `GET /api/shelf/recommendations`
  (seeds = every book on the user's normal shelves — never the Exclusion
  shelf). Both exclude **every book on any of the user's shelves**, Exclusion
  included, from the results and return 8 books. The UI lists core matches first and exploratory ones under
  **"You might also enjoy"**, with the shelf's genre clusters as chips.
- Catalog vs. KB: the catalog is seeded from the KB's own documents (T2), so it
  holds all 488 KB books (+ 30 curated) and hits resolve to catalog references.

---

## 5A. "What's my next read?" — conversational agent box (built)

A persistent text box on **all screens** that sends the user's free-text request to
the top-level **agent loop** — the component that *selects and calls tools*, i.e. the
showcase for the AI-workflow pillar. **Read-only:** it recommends, it never mutates
shelves/ratings. This one feature carries four sub-requirements at once: structured
extraction (intent → tool args), **agent tool-selection**, a useful result, and
visible uncertainty.

**Sign-in required.** The assistant is for authenticated users only: `POST
/api/assistant` is gated by `require_user` (401 when signed out), and the UI hides
the launcher entirely for signed-out visitors. Conversation sessions, history and
the reader's library/exclusions are all per-user — there is no anonymous mode.

**Registered tools (user-selected set):** `search_catalog`, `get_book_details`,
`find_similar`, `find_similar_by_bookshelf`, `get_user_library`.
- `get_reviews_and_guides` is **deliberately not** in this set for now (Q17 —
  skipped), so the box's "why" is not citation-grounded; the graded grounded-answers
  evidence lives on the book page instead.

**Context the agent receives:**
- **Conversation only.** The agent's prompt context is the conversation itself — the
  last `k` **messages**, counting user inputs *and* assistant responses together,
  loaded from Postgres (§6). Enables follow-ups ("make them shorter", "less fantasy").
  **No screen/focus context** — the box does not know the current book or shelf
  (Q18 — decided against). `k` = env `AGENT_HISTORY_K`, default **5**. All messages
  are persisted; `k` bounds only the context window, not storage.
- **Library via tools, not ambient context.** The agent calls `get_user_library`
  when it needs the reader's shelves/ratings; the **post-agent validation step**
  still enforces exclusion (never recommend already-read/owned) server-side,
  regardless of what was in the prompt.

**Flow example:** *"something like Dune but short, no gore"* →
`get_book_details(Dune)` → `find_similar` → length filter → `get_user_library`
(exclude owned) → shortlist. Multi-seed (*"based on my shelf"*) → gather `Book`s via
`get_user_library` → `find_similar_by_bookshelf`.

**Output:** the shared shortlist-card shape + a "couldn't answer" note. The existing
**post-agent validation step** runs on this path (books resolve to real entries;
already-read dropped).

**Risks:** latency 10–40 s → needs a loading state (streaming still deferred); the
agent may skip tools → the validation gate enforces output; tool-selection should be
made **visible** (a short trace of the tools fired) as proof the agent chose them.

---

## 6. Data model

**Canonical book set is keyed by ISBN.** Two representations, joined by ISBN:

- **Postgres** — structured rows the web UI reads. **All PKs and FKs are
  `integer` identity keys**; `isbn` is a natural key (**unique**), never a key column.
  KB retrieval hits return an `isbn` → look up `book_id` to use internally.

  **`books`** (stage 1)

  | column | type | notes |
  |---|---|---|
  | `book_id` | `integer` identity **PK** | synthetic internal id |
  | `isbn` | `text` **unique not null** | ISBN-10; join target for KB hits + Open Library |
  | `isbn13` | `text` null | unreliable per design |
  | `title` | `text` not null | |
  | `author` | `text` null | (`authors text[]` if co-authors matter) |
  | `description` | `text` null | blurb; part of `find_similar_by_bookshelf`'s per-cluster KB query |
  | `pages` | `int` null | |
  | `published_year` | `int` null | |
  | `language` | `text` null | |
  | `genres` | `text[]` null | GIN-indexed; feeds `search_catalog` filter |
  | `content_warnings` | `text[]` null | source TBD (Q1); may become AI-estimated. **Not surfaced in the web API/UI yet** — deferred; the column stays, the field returns in a later slice. |
  | `cover_url` | `text` null | |
  | `source` | `text` null | provenance, e.g. `openlibrary` |
  | `details_fetched_at` | `timestamptz` null | `get_book_details` cache freshness |
  | `created_at` / `updated_at` | `timestamptz` | |

  **`users`** (stage 1)

  | column | type | notes |
  |---|---|---|
  | `user_id` | `integer` identity **PK** | |
  | `email` | `text` unique not null | |
  | `display_name` | `text` null | |
  | `created_at` | `timestamptz` | |

  **`user_details`** (stage 1) — authentication sidecar, **1:1** with `users`.

  | column | type | notes |
  |---|---|---|
  | `user_id` | `integer` **PK & FK → `users`** | 1:1; `ON DELETE CASCADE` |
  | `password_hash` | `text` not null | from bcrypt / argon2 / scrypt |
  | `salt` | `text` null | optional — bcrypt/argon2 embed the salt in the hash string |
  | `blocked_until` | `timestamptz` null | null = not blocked; set on lockout |
  | `updated_at` | `timestamptz` | |

  - Split from `users` so auth secrets stay out of the row the web UI reads.
  - Optional: `failed_attempts smallint` to drive `blocked_until`.

  **Auth (implemented, stage 1):** email + password login with **bcrypt** hashes
  in `user_details`, kept in a signed **HTTP-only session cookie**
  (`SessionMiddleware`). **No roles yet** — every signed-in user has the same
  access; **sign-up is public**. Books + details are public; shelves, ratings and
  recommendations require sign-in. The base account `admin@bookshelf.com` is **seeded as
  `user_id = 1`** (password a committed bcrypt hash — a demo credential).
  `blocked_until` / `failed_attempts` lockout is not wired yet.

  **`shelves`** + **`shelf_books`** (migrations `0003`, `0004`) — **a user's
  library is their shelves.** Shelves replaced the four statuses (read / owned /
  want to read / disliked) in migration `0004`. A book may sit on **several**
  shelves (many-to-many).

  - `shelves(shelf_id PK, user_id FK, name, is_exclusion, created_at, updated_at)`;
    `unique (user_id, lower(name))`; partial unique index: one `is_exclusion` per user.
  - `shelf_books(shelf_id FK, book_id FK, added_at, PK(shelf_id, book_id))`.
  - **Exclusion shelf:** every user has exactly one built-in shelf named
    "Exclusion" (`is_exclusion = true`), created by a trigger on `users` insert.
    Its books are **never used as taste and never recommended**. The API refuses
    to rename or delete it, or to recommend from it.
  - **Rule (DB trigger on `shelf_books`):** a book is on Exclusion **or** on
    normal shelves, never both — adding it to one kind removes it from the other.
  - Deleting a normal shelf never deletes books; they just leave that shelf.
  - Migration `0004` data: disliked → Exclusion; read → "Read"; want_to_read →
    "Want to read"; owned books on no shelf → "Owned"; existing shelves kept.
    (A pre-migration copy was kept as the database `bookrec_backup_pre0004`.)

  **`user_library`** (stage 1, slimmed in `0004`) — the user's own **rating and
  review** of a book, independent of shelves. Serves the n8n candidate query
  (rated ≥ 4) and the book page's "Your rating".

  | column | type | notes |
  |---|---|---|
  | `user_id` | `integer` FK → `users` | PK part |
  | `book_id` | `integer` FK → `books` | PK part |
  | `rating` | `smallint` null | `CHECK (rating BETWEEN 1 AND 5)` |
  | `review` | `text` null | **user** review (≠ KB editorial review) |
  | `added_at` / `updated_at` | `timestamptz` | |

  - PK `(user_id, book_id)`; index `(book_id, rating)` for the n8n candidate query.
  - A row exists only while it holds a rating or a review.

  **`results`** (*stage 2*, deferred D2) — persisted recommendation runs.

  | column | type | notes |
  |---|---|---|
  | `result_id` | `integer` identity **PK** | |
  | `user_id` | `integer` null FK → `users` | |
  | `request` | `jsonb` | taste profile + filters + seed/photo ref |
  | `recommendations` | `jsonb` | `[{book_id, isbn, why, source_ids[], confidence, flags[]}]` |
  | `unresolved` | `jsonb` null | unmatched spines / "couldn't answer" |
  | `created_at` | `timestamptz` | |

  **`uploads`** (*stage 2*, deferred D1) — `upload_id integer` **PK**,
  `user_id integer` FK → `users`, `s3_key text`, `content_type text`, `created_at timestamptz`.

  **`agent_sessions`** (stage 1) — a conversation thread for the "next read" box.

  | column | type | notes |
  |---|---|---|
  | `session_id` | `integer` identity **PK** | one row per conversation |
  | `user_id` | `integer` FK → `users` | `ON DELETE CASCADE` |
  | `title` | `text` null | optional label (e.g. first message) |
  | `created_at` / `updated_at` | `timestamptz` | |

  **`agent_turns`** (stage 1, **as built** — migration `0005`) — one row per
  **turn**: the reader's input plus the agent's structured output as JSON.

  | column | type | notes |
  |---|---|---|
  | `turn_id` | `integer` identity **PK** | |
  | `session_id` | `integer` FK → `agent_sessions` | `ON DELETE CASCADE`; index `(session_id, created_at)` |
  | `user_input` | `text` not null | what the reader typed |
  | `output` | `jsonb` not null | `{reply, recommendations:[{book_id,isbn,title,author,cover_url,why,excluded}], unverified:[isbn], tool_trace:[{tool,input}], notes:[]}` |
  | `created_at` | `timestamptz` | |

  - The agent loads the **last `AGENT_HISTORY_K` (default 5) messages** — counting
    user inputs and assistant replies together, a turn being two — ordered by
    `created_at` (only `user_input` + `output.reply` are replayed into context).
    **All** turns are persisted; `k` bounds only the context window, not storage.
  - A *session* groups one conversation; the box continues the user's current
    session, with a "new chat" control starting another (Q19).
- **Bedrock KB** — per-book text chunks (summary/review/guide) with **`isbn` in
  chunk metadata** so a retrieval hit resolves back to a `Book`.
  - *As built:* a **managed** knowledge base (requests use
    `managedSearchConfiguration`), one document per book (`doc_type: "book"`,
    1–2 chunks) holding title, author, genres, editorial review quotes and
    author bio. No separate guide documents yet.
  - Metadata `isbn` is the **ISBN-10**; `isbn13` exists but is unreliable. Each
    chunk's `_chunk_id` is the citation `source_id`.

**Consistency rule:** ingest the same book list into both. A KB hit whose ISBN is
missing from the DB falls back to Open Library rather than erroring.

**Two kinds of "review" — keep separate:**
- *User review* (user-generated, in `user_library`).
- *Editorial review* (RAG grounding, in the KB). Do not merge without a deliberate decision.

---

## 7. n8n outbound pipeline — "find interested readers"

Given a target book (review + description), find and email readers likely to want it.

```
trigger (new book added / manual)
  → Bedrock Retrieve: query KB with target's review+description → similar ISBNs
  → Postgres: resolve ISBNs, drop unknowns
  → Postgres: users who rated those ≥ 4, minus users who already have the target
  → (optional) LLM node: personalize email ("Because you loved X…")
  → SES: send
```

**Logic:** content-based collaborative filtering — "users who liked books *similar
to X* are candidates for X."

**Guardrails:**
- **Never email real people.** Use seeded test users with addresses you control;
  run **SES in sandbox mode** (verified recipients only).
- Dedup + rank candidates by affinity; **cap volume**.
- Exclude users who already read/own/wishlisted the target.

**Scope:** independent of the main app; **stage 2** (additive).

---

## 8. AWS deployment

**Topology: two EC2 instances** (self-managed), plus the already-deployed managed
Bedrock KB. Decided this session.

```
                    Internet
                       │ 80/443
         ┌─────────────▼──────────────┐
         │  webapp EC2 (public subnet) │  Elastic IP
         │  nginx → React build        │  instance role: Bedrock, ECR pull,
         │  nginx /api → FastAPI (Docker)│  SSM params, CloudWatch
         └─────────┬───────────┬───────┘
          5432 ──▶ │           │ ──▶ Bedrock KB `Retrieve` + model calls (managed)
         ┌─────────▼──────────┐│      Open Library (external HTTPS)
         │  db EC2            ││
         │  Postgres (self-   ││
         │  managed)         ││
         │  SG: 5432 from    ││
         │  webapp-sg only   ││
         └───────────────────┘
```

| Role | Service | Notes |
|------|---------|-------|
| Hosting — app | **webapp EC2** (t3.small), Docker + nginx | nginx serves the React build and reverse-proxies `/api` → FastAPI container. Elastic IP for a stable address. |
| Hosting — db | **db EC2** (t3.small), **self-managed PostgreSQL** | Not RDS (deliberate, hands-on learning). *We own backups + patching* → `pg_dump` → S3 (or EBS snapshots) on a cron. |
| Storage — knowledge | **Bedrock KB** (+ its managed vector store) | **Already deployed** (managed KB; §6). Its always-on vector store is the main recurring cost. |
| Storage — structured | **Postgres on db EC2** | catalog + users/lists/shelves/ratings/agent sessions. |
| Storage — uploads/results | **S3** | *Deferred to stage 2* (D1/D2). |
| Image registry | **Amazon ECR** | repos e.g. `bookrec-backend` (+ `bookrec-frontend` if built separately). |
| Models | **Bedrock** (Claude + embeddings) | Called from the webapp via its **IAM instance role** — no static keys at runtime. |
| Access & ops | **IAM instance roles**, security groups, **SSM Parameter Store** (runtime secrets), **CloudWatch** logs | See network + secrets below. |
| Cost & cleanup | Estimate + teardown | 2× t3.small + Elastic IP + ECR storage + the KB vector store (the always-on item). Tear down after grading. |

### 8A. Networking & access

- **VPC** with a **public subnet** (both instances, for an SSH-based workflow);
  db has no public ingress except SSH from a fixed IP.
- **Security groups:**
  - `webapp-sg`: inbound **80/443 from anywhere**, **22 from your IP only**.
  - `db-sg`: inbound **5432 from `webapp-sg` only**, **22 from your IP only**.
- **Elastic IP** on the webapp so the address survives restarts.
- **IAM instance role (webapp):** `ecr:GetAuthorizationToken` + pull,
  `bedrock:InvokeModel`, `bedrock-agent-runtime:Retrieve`, read its SSM params,
  `logs:*` (CloudWatch). No AWS keys on the box.
- **Runtime secrets** (DB password, etc.) in **SSM Parameter Store** (SecureString),
  fetched at startup — never in the image or git.

### 8B. Containers → ECR → deploy (SSH)

Chosen mechanism this session: **SSH-based** deploy, **static access keys** for the
CI→AWS step, **ECR** registry. Trade-offs: SSH keeps port 22 open (lock it to your
IP); static keys are a stored credential — mitigate with a **least-privilege IAM
user** scoped to ECR push only, and rotate it.

**Order of work (AWS side first, GitHub after):**
1. Create ECR repo(s).
2. Build the app image locally → `docker push` to ECR.
3. On webapp EC2: `docker compose pull && up -d` (pull via the instance role) → hit
   the Elastic IP → confirm it serves and reaches db. **This manual deploy is the
   validation gate** — only automate once it works by hand.
4. Then wire **GitHub Actions**: on push → build → push to ECR (static-key IAM user)
   → SSH into webapp → `compose pull && up -d` → run DB migrations → health check.

*Current focus: steps 1–3 (containers to ECR + first manual deploy); GitHub Actions
(step 4) comes after.*

---

## 9. Technology choices (to justify in writeup)

- **Backend:** Python + FastAPI.
- **Frontend:** React.
- **Agent:** plain tool-calling loop (no heavyweight agent framework at top level).
- **Vision + reasoning:** Claude (latest available).
- **Pipeline-in-a-tool:** LangGraph (inside `parse_bookshelf` and
  `find_similar_by_bookshelf`), with plain-Python nodes over the raw Anthropic SDK +
  boto3 — **not** LangChain.
- **RAG:** Bedrock Knowledge Base (`Retrieve`), per-book chunking; embeddings via
  Titan v2 or Cohere on Bedrock.
- **Metadata:** Open Library API, called directly for now; a Postgres cache
  comes with the catalog DB.
- **DB:** PostgreSQL (with `pgvector` if KB store is Aurora).
- **Automation:** n8n (outbound pipeline) + SES for email.
- **Config (.env):** `AGENT_HISTORY_K` — how many prior session messages the "next
  read" agent loads into context (default **5**).
- Record exact versions at build time.

---

## 10. Build plan

**Stage 1 — core interactive app (MVP):**
1. DB schema + seed a few hundred books (from Open Library).
2. Ingest per-book text into Bedrock KB (shared by `find_similar` + reviews).
3. Backend: agent loop + the 9 tools (`parse_bookshelf` and
   `find_similar_by_bookshelf` with their LangGraphs).
4. Post-agent validation step (exists / constraints / cited / not-already-read).
5. React UI: submit taste + bookshelf photo, render cited result cards with flags.
6. Deploy on EC2; wire IAM, security group, CloudWatch; minimal eval set.

**Stage 2 — additive:**
- S3 for uploads + persisted results.
- n8n "find interested readers" pipeline (SES sandbox).
- Richer eval harness; reviewer accept/reject actions; availability tool.

---

## 11. Non-negotiables (so pillars don't silently fail)

- Every recommended book resolves to a **real** catalog/Open Library entry.
- Every "why" is bound to a **`source_id`**; validation rejects uncited claims.
- **Uncertainty is visible** (`unresolved` spines, low-confidence flags, "couldn't answer").
- Failures **degrade** (partial results + flag), never crash.
