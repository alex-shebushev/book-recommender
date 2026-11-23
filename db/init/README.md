# DB init scripts

SQL files here are executed by the Postgres container **only on first
initialization** (when the `pgdata` volume is empty), in **alphabetical order**.

Both files are **GENERATED** by [`../build_init_sql.py`](../build_init_sql.py) —
do not edit them by hand:

- `01_schema.sql` — every migration in [`../migrations/`](../migrations),
  concatenated in order (migrations stay the source of truth).
- `02_seed.sql` — the full catalog (curated + `../kb_catalog.json`), the demo
  user, its ratings and its shelves.

So a fresh `docker compose up -d` comes up with schema **and** data, no manual
`migrate.py`/`seed.py` step. After changing a migration or the seed data,
regenerate and recreate the volume:

```bash
python ../build_init_sql.py
cd ../.. && docker compose down -v && docker compose up -d
```

> The host-run path (`python ../migrate.py && python ../seed.py`) still works
> against an existing volume and reads the same data.
