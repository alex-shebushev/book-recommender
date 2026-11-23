# Book Recommender — Open Questions & Deferred Items

> Concerns raised during design that are unresolved, deferred, or need a decision
> at build/deploy time. Companion to [`DESIGN.md`](DESIGN.md).

Legend:
- **OPEN** — not yet decided.
- **DEFERRED** — intentionally postponed; several are **required for final submission**.
- **DECIDE AT BUILD / DEPLOY** — direction known, concrete choice made when we get there.
- **TODO** — agreed small piece of work, not started.

---

## To implement (small items)

| # | Item | Status | Notes |
|---|------|--------|-------|
| T1 | **"Hide excluded books" checkbox on recommendations** | DONE | A remembered (localStorage) checkbox on both the recommendations panel **and** the book-page "Find similar". **Checked (default):** Exclusion-shelf books are hidden. **Unchecked:** they are shown grayed, with an "On your Exclusion shelf" badge. If hiding leaves none, the panel shows **"All results are in your exclusions."** Backend: `find_similar` gained `flag_isbns` and `find_similar_by_bookshelf` gained `soft_exclude` — Exclusion books are kept and tagged `excluded=True` (not dropped); the API splits the library into hard `exclude` (normal shelves, dropped) and `soft_exclude` (Exclusion shelf, flagged), requests **10** (excluded hits count toward it), and the `/api/books/{id}/similar` route now uses the signed-in reader's Exclusion shelf. The checkbox is **always shown** next to the Find similar / Get recommendations buttons; it only changes what you see when a result is actually on your Exclusion shelf, which (now that T2 has loaded the KB's books) happens whenever a result is on your Exclusion shelf. |
| T2 | **Load the KB's book set into Postgres** | DONE | The catalog now holds **517 books** (30 curated + 488 from the KB). `db/build_catalog_from_kb.py` lists the KB's ingested documents, reads each book's `.metadata.json` sidecar (isbn, title, author, year, language, genres, pages) from S3 and a trimmed blurb from the `.txt`, and writes committed **`db/kb_catalog.json`** (ISBN-10 keyed). `seed.py` loads it (curated `DO NOTHING`, then KB `DO UPDATE` — KB wins). `db/build_init_sql.py` writes `db/init/01_schema.sql` + `02_seed.sql` so a fresh Docker volume comes up fully populated with no manual step. Recommendations now resolve to clickable catalog references (verified 10/10). Leftover: descriptions are promotional (Q14); duplicate editions by ISBN still possible (Q9). |
| T3 | **Add to a bookshelf from a photo** | DONE | Built as `app/tools/parse_bookshelf.py` — a LangGraph tool (extract via Claude vision → deterministic match), **in-memory only** (no S3, D1). Matches against the **catalog DB** (not the KB — it's a superset, T2), with a language cascade (detected → en → any) and a canonical/English `title_en` from vision to bridge languages; author is the anchor. `POST /api/shelves/{id}/parse-photo` (multipart, Exclusion refused) + `POST /api/shelves/{id}/books/bulk`; ShelfPage `PhotoImport` panel with review/checkbox/delete/confirm. **Decision change:** unmatched spines are **omitted** (not shown) per the user, with a "read N, matched M" note. Offline tests: `tests/test_parse_bookshelf.py`. **Not yet run live** (needs ANTHROPIC_API_KEY). Dropped from original scope: "new shelf" creation (adds to the open shelf only) and spine accuracy is still Q7. |
| T4 | **"What's my next read?" conversational agent box** | DONE | Built: `POST /api/assistant` (`require_user`) runs a bounded tool-use loop (`app/assistant.py`) over 6 tools (`app/tools/agent_tools.py`: the 5 data tools + `present_recommendations` finishing tool). History in Postgres `agent_sessions` + `agent_turns` (migration `0005`, one row per turn, `output` JSONB); `agent_store.py` loads the last `AGENT_HISTORY_K` (default 5) messages. Server **verifies picks against Postgres** (drops unverified → `notes`), applies exclusion (drop normal-shelf, flag Exclusion). Global React panel `AssistantBox` (pages.js) with the shared hide-excluded toggle + tool-trace. Offline tests: `tests/test_assistant.py`. Decided: no grounding tool (Q17), no focus context (Q18), `k` counts messages + session threads (Q19). **Not yet run live** (needs ANTHROPIC_API_KEY + AWS + migration `0005` applied). |

---

## Deferred (stage 1 cuts — but required for the final submission)

These were cut to keep stage 1 small. They are **explicit rubric requirements**, so
they must come back before submission — track, don't forget.

| # | Item | Status | Notes |
|---|------|--------|-------|
| D1 | **Persist uploads to S3** | DEFERRED | Stage 1 passes the image to `parse_bookshelf` in memory only. Rubric Storage role says "persist uploads" — add S3 in stage 2. |
| D2 | **Persist results** | DEFERRED | Store each recommendation run (shortlist + flags) to DB/S3. Rubric Storage role lists "results". |
| D3 | **Evaluation harness** | PARTLY DONE | First real eval: `app/scripts/eval_bookshelf_query.py` — LLM-judged precision@8 and run-to-run stability of bookshelf recommendations over 6 test shelves; it drove the KB-query redesign (0.38 → 0.72, stability 0.31 → 1.00). Still to do: the rest of the full eval (citation coverage, constraint-compliance %, hallucination rate) for the agent's answers — the "Validation & evaluation" pillar is graded. |

---

## Open questions

| # | Question | Status | Context / leaning |
|---|----------|--------|-------------------|
| Q1 | **Content-warning data source** | OPEN | Open Library has no reliable content warnings. Options: a dataset, or LLM-derived from the summary. If LLM-derived it's approximate → label as "AI-estimated" in the UI (feeds the uncertainty pillar). |
| Q2 | **Citation integrity end-to-end** | DECIDE AT BUILD | KB returns `source_id`s, but LLMs routinely drop/mismatch them. Bind each claim to its `source_id` in the output schema; validation rejects uncited "why". Don't trust the model to echo them. |
| Q3 | **Who is the "human reviewer"?** | OPEN | Brief frames output "for a human reviewer". Likely = the end user approving/rejecting recs. Decide explicitly — it determines where confidence badges & "couldn't answer" live and what the human can *do*. |
| Q4 | **Agent model: Anthropic API vs Claude-on-Bedrock** | DECIDE AT DEPLOY | API → key in Secrets Manager. Bedrock → IAM-only, no key, unified with the KB. Either defensible; note choice in tech-choices writeup. "2 models OK" (agent/vision model + KB embeddings model). |
| Q5 | **Vague-input fallback** | OPEN | Clarify-loop was dropped (one-shot API). So vague input needs: reject-with-guidance **or** best-effort + flagged assumptions. Pick one so "validate inputs" still exists. |
| Q6 | **Tool-failure degrade path** | DECIDE AT BUILD | Define behavior when a tool (Bedrock/Open Library) times out: partial results + flag, never crash. Part of the validation pillar. |
| Q7 | **`parse_bookshelf` match scope & accuracy** | OPEN (experiment) | Spine reading is hard; expect a modest hit rate and lean on `unresolved`. Match against Open Library search (broad), not only the small local catalog. Accuracy is something to experiment with, per project goal. |
| Q8 | **Vector store choice (KB)** | DECIDE AT DEPLOY | OpenSearch Serverless has a steep always-on minimum; prefer **Aurora pgvector** for a cheap demo. This is the main line in the cost estimate + the key cleanup item. |
| Q9 | **One book has many ISBNs** | OPEN | A single title has many ISBNs (editions, formats, translations, box sets). Undermines ISBN as a clean key: fragments the catalog/KB, breaks dedup and self-exclusion, and the user never knows which ISBN. Decide a canonical identity (e.g. a work-level id, or cluster editions under one chosen ISBN-13). ISBN stays an internal canonical/exclusion key, not a user-facing input (find_similar takes query text, not an ISBN). |
| Q10 | **LLM clustering test — gating for `find_similar_by_bookshelf`** | RESOLVED (gate passed) | Ran `app/scripts/clustering_gate.py` on a 7-book mixed shelf. Clusters coherent, compound genres handled, missing genre inferred, blurbs blurb-style. **Not stable at first:** Dune landed in "epic fantasy" in 1 of 2 runs. This SDK build accepts no `temperature`, so a prompt rule (keep sci-fi and fantasy apart) fixed it — membership then identical across 3 runs. Labels still vary in wording, so nothing keys off them. Proven on one shelf only; a crossover-heavy shelf could still wobble. |
| Q11 | **Exploratory flag + floor size** | DECIDED | 1 floor slot per cluster, rest proportional to cluster size; a cluster smaller than the largest is `exploratory` and its hits carry `exploratory=True`. The UI lists them under **"You might also enjoy"** below the core matches, and shows exploratory clusters as dashed chips. |
| Q12 | **KB scores can't gate "no strong match"** | OPEN | On the managed KB an off-topic query ("quarterly tax accounting rules") scored 0.86 at the top, above most real book queries, and with reranking off scores are purely relative (0.731 / 0.269 for every two-passage book). So `min_score` cannot tell a weak match from a strong one. Needs another signal (e.g. the agent judging the snippet) for the "missing information" path. |
| Q13 | **KB files books under ISBN-10; `isbn13` metadata is unreliable** | PARTLY FIXED | The app's shared form is ISBN-13. **Exclusion fixed:** `find_similar` now compares every form (raw, ISBN-10, ISBN-13), so excluding Hyperion by its ISBN-13 works (verified on the live KB). **Still open:** some `isbn13` metadata values have 14 digits or belong to another edition — fix at ingestion. `find_similar` still *returns* the KB's ISBN-10, which is what `books.isbn` holds. |
| Q15 | **Catalog and KB hold different book sets** | RESOLVED (T2) | The catalog is now seeded from the KB's own documents (488 books), so recommendations resolve to catalog references instead of "Not in this catalog". The two still aren't guaranteed identical (curated-only books remain, and the KB can change), but `db/build_catalog_from_kb.py` re-syncs on demand. |
| Q16 | **Disliked books seed My-bookshelf recommendations** | RESOLVED | Statuses were replaced by shelves (migration `0004`) with a built-in **Exclusion** shelf: its books are never used as taste and never recommended, and the former Disliked books moved there. "Get recommendations" on My bookshelf now seeds only from normal shelves. |
| Q14 | **"Reviews" are publisher praise; no guides in the KB** | OPEN | Each book has one document: review quotes (mostly marketing blurbs), author bio, sometimes an excerpt. `get_reviews_and_guides` returns it faithfully, but grounding a "why you'd like it" on praise blurbs is weak evidence. Decide whether to ingest real summaries/guides (told apart by `doc_type`) or label the source as promotional. Some documents also contain junk markup (leftover Word styles). |
| Q17 | **Next-read box has no grounding tool** | RESOLVED (skipped) | Decided to **skip** adding `get_reviews_and_guides` to the box for now, so the box's "why" is not citation-grounded. The graded grounded-answers evidence lives on the **book page** instead. Reopen if the box itself must cite. |
| Q18 | **Focus context for the next-read box** | RESOLVED (none) | Decided: **no screen/focus context** — the box works only from the conversation (user inputs + responses), not the current book/shelf. The agent reaches the reader's library via the `get_user_library` tool; exclusion is enforced in the post-agent validation step, not injected as ambient context. |
| Q19 | **Agent session scoping & history retention** | RESOLVED | `k` counts **individual messages** — user inputs *and* assistant responses together — not turns. **All** messages are stored; only the last `k` load into context. A *session* = one conversation thread; "new chat" starts another. |

---

## Decisions already made (for reference)

- Top-level orchestration = **plain agent loop + tools** (no outer LangGraph).
- LangGraph used inside **two tools** — `parse_bookshelf` and `find_similar_by_bookshelf` — never at the top level. Plain-Python nodes over the raw Anthropic SDK + boto3; **not LangChain** (it would re-wrap primitives we already have).
- `find_similar` (query-text primitive) is **implemented**: pure boto3 KB `Retrieve`, with threshold / exclude / dedup client-side.
- `find_similar_by_bookshelf` (renamed from `find_similar_by_book`) = code-orchestrated LangGraph tool over `find_similar`; it takes the shelf as **`Book`s plus an `exclude` list of books** (the caller resolves them — the web API uses catalog rows), so it no longer calls `get_book_details` itself. LLM only in the `cluster` node. Per-cluster queries (**true OR**, not a centroid) built **without an LLM** from the cluster's genres + its books' descriptions (an LLM blurb was measured and dropped: precision 0.38 vs 0.72); **floor + proportional** slot allocation; **genre-first with LLM gap-fill**. **Implemented** — exclusion ended up client-side through `find_similar` plus a title check for other editions, not a server-side KB filter (which would be edition-blind anyway, Q9). See DESIGN §5 "As built".
- `find_similar` + `get_reviews_and_guides` share **one Bedrock KB**. It is a
  **managed** knowledge base, so both send `managedSearchConfiguration`
  (`vectorSearchConfiguration` is rejected).
- `get_reviews_and_guides` is **implemented**: takes an ISBN in any form, pins
  the book with a metadata filter on `isbn`/`isbn13`, turns the KB's reranking
  off so every passage of the book comes back (the default keeps only one), and
  returns `ReviewDoc`s whose `source_id` is the Bedrock chunk id. An empty list
  means the KB has nothing on that book.
- `get_book_details` is **implemented**: Open Library over plain HTTP, no framework, no cache yet (a Postgres cache comes with the catalog DB). It always reports the book under its ISBN-13.
- The shared ISBN form is **ISBN-13, digits only** (`app/isbn.py`). `get_book_details` follows it; `find_similar` compares exclusions in every form (Q13). This settles the form, not the edition problem in Q9.
- `check_availability` = plain async HTTP fan-out (no framework).
- Canonical book set keyed by **ISBN**; KB (text/embeddings) + Postgres (structured)
  are two representations of it.
- **Shelves are the library** (migration `0004`): the four statuses are gone; every user has a built-in **Exclusion** shelf (never taste, never recommended; can't be renamed/deleted), and a DB trigger keeps a book on Exclusion **or** normal shelves, never both. `user_library` now holds only the per-book rating/review (the n8n pipeline reads ratings).
- n8n "find interested readers" pipeline = **stage 2**, SES sandbox + seeded users only.
- Hosting = **EC2 with containers**.

---

## Rule of thumb applied throughout

> **Does the step need the model to reason or produce natural language?**
> Yes → graph / LLM.  No → plain code.

Reaching for LangChain/LangGraph to make HTTP requests is an anti-pattern — the
frameworks are for chaining LLM calls and branching on their output.
