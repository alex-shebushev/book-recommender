# Database

Local Postgres runs via [`../docker-compose.yml`](../docker-compose.yml). The schema
is managed with plain versioned SQL migrations in [`migrations/`](migrations), applied
by [`migrate.py`](migrate.py), which records applied files in a `schema_migrations`
table (so re-running only applies new ones).

## First run

A **fresh** container (empty data volume) initializes itself from the generated
`init/01_schema.sql` + `init/02_seed.sql` — schema **and** the full catalog
(30 curated + 488 from the Knowledge Base), the demo user and its shelves — so
there is nothing to run by hand:

```bash
cp ../.env.example ../.env        # set the DB password
cd .. && docker compose up -d     # fresh volume -> schema + catalog loaded
```

To do it by hand instead (e.g. against an existing volume):

```bash
cd db && pip install -r requirements.txt
python migrate.py                 # schema
python seed.py                    # catalog + demo data (idempotent)
```

## Commands

```bash
python migrate.py            # apply all pending migrations (safe to re-run)
python migrate.py --status   # show applied / pending, change nothing
python seed.py               # load catalog + demo data (safe to re-run)
```

## Catalog data

The catalog is **517 books**: 30 curated (hand-written descriptions) plus 488
from the Bedrock Knowledge Base, so recommendations resolve to real catalog
entries instead of "Not in this catalog".

- [`seed_data.json`](seed_data.json) — the curated books + the demo user, its
  ratings and shelves.
- [`kb_catalog.json`](kb_catalog.json) — the 488 KB books (generated; committed).
- [`build_catalog_from_kb.py`](build_catalog_from_kb.py) — rebuilds
  `kb_catalog.json` from the KB's S3 documents. **Needs AWS credentials** +
  `BEDROCK_KB_ID`. Re-run only when the KB changes.
- [`build_init_sql.py`](build_init_sql.py) — regenerates `init/01_schema.sql`
  (the migrations concatenated) and `init/02_seed.sql` (all the seed data) from
  the migrations + the two JSON files. **Re-run after any migration or seed-data
  change**, then recreate the volume to apply:
  ```bash
  python build_catalog_from_kb.py   # optional: refresh the KB books (needs AWS)
  python build_init_sql.py
  cd .. && docker compose down -v && docker compose up -d
  ```

Connection comes from `DATABASE_URL`, or is built from the `POSTGRES_*` vars that
the compose `.env` already defines (host defaults to `localhost`).

## Adding a schema change

1. Create `migrations/NNNN_short_description.sql` using the next number.
2. Write forward-only DDL — **do not** edit an already-applied migration; add a new one.
3. **Do not** put `BEGIN`/`COMMIT` in the file; the runner wraps each file in a
   transaction and appends the `schema_migrations` bookkeeping atomically.

## Notes

- Migrations are **forward-only** (no down scripts) — fine for this project.
- Stage-2 tables (`results`, `uploads`, per DESIGN.md §6) land in a later migration
  when that work starts.
- `init/01_schema.sql` and `init/02_seed.sql` are **generated** by
  `build_init_sql.py` — do not edit them by hand. Migrations in `migrations/`
  remain the source of truth for schema; the generator just concatenates them
  for the container's first-boot init.
