"""Postgres access for the web API (catalog / book details / bookshelf).

The first database layer in `app/`: the agent tools talk to Open Library and
Bedrock, nothing here touched Postgres before. A small connection pool is opened
at startup and closed at shutdown (see api.py's lifespan).

Connection string resolution, in order:
  1. DATABASE_URL — set in the app's `.env` for host dev, or by docker-compose
     for the container (points at `db:5432`). This is the normal path now.
  2. Built from POSTGRES_* — fallback only. These live in the repo-root `.env`
     (POSTGRES_USER/PASSWORD/DB/PORT), which we also load; host + port default to
     localhost:5544 (the host-published port from docker-compose).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Load the app's own .env (API keys) and the repo-root .env (POSTGRES_*). Both
# are optional; in the container the platform sets the env directly.
load_dotenv(Path(__file__).resolve().parent / ".env")  # app/.env, from any cwd
_ROOT_ENV = Path(__file__).resolve().parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)


def _database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "bookrec")
    password = os.getenv("POSTGRES_PASSWORD", "")
    db = os.getenv("POSTGRES_DB", "bookrec")
    host = os.getenv("POSTGRES_HOST", "localhost")
    # Host-run dev reaches the container via the published port (5544 by default);
    # inside the compose network DATABASE_URL is set instead, so this is host-only.
    port = os.getenv("POSTGRES_PORT", "5544")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


# One pool per process. `dict_row` so every query yields dicts (easy to serialize).
# `open=False` + explicit open() in the lifespan avoids opening at import time.
pool = ConnectionPool(
    conninfo=_database_url(),
    min_size=1,
    max_size=10,
    kwargs={"row_factory": dict_row},
    open=False,
)
