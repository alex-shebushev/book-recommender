#!/usr/bin/env python3
"""Apply pending SQL migrations to the Book Recommender database.

Every ``*.sql`` file in ``db/migrations/`` is applied once, in filename order,
each inside its own transaction, and recorded in ``schema_migrations``. Running
it again applies only new files, so it is safe to re-run (an "upgrade").

Connection (first that is set wins):
  * ``DATABASE_URL``  e.g. postgresql://bookrec:pwd@localhost:5432/bookrec
  * otherwise built from POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB /
    POSTGRES_HOST (default localhost) / POSTGRES_PORT (default 5432) — the same
    variables the local docker-compose .env already defines.

Usage:
  pip install -r db/requirements.txt
  python db/migrate.py             # apply all pending migrations
  python db/migrate.py --status    # list applied / pending, change nothing
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import pq

DB_DIR = Path(__file__).resolve().parent
MIGRATIONS_DIR = DB_DIR / "migrations"


def _load_env() -> None:
    """Best-effort load of the compose .env next to docker-compose.yml."""
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv(DB_DIR.parent / ".env")


def dsn() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("POSTGRES_USER", "bookrec")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    name = os.getenv("POSTGRES_DB", "bookrec")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pwd}@{host}:{port}/{name}"


def _run_script(conn: psycopg.Connection, sql: str) -> None:
    """Run a whole migration script via the simple-query protocol.

    psycopg's normal ``execute()`` uses the extended protocol, which rejects
    multi-statement strings and is awkward with dollar-quoted function bodies.
    ``PQexec`` (simple protocol) runs the whole string at once; the caller wraps
    it in BEGIN/COMMIT so the migration and its bookkeeping are atomic. On any
    error the server aborts the transaction and COMMIT is never reached, so the
    whole file rolls back.
    """
    result = conn.pgconn.exec_(sql.encode("utf-8"))
    if result.status == pq.ExecStatus.FATAL_ERROR:
        raise RuntimeError(result.error_message.decode(errors="replace").strip())


def main() -> int:
    _load_env()
    status_only = "--status" in sys.argv

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No migrations found in {MIGRATIONS_DIR}")
        return 0

    try:
        conn = psycopg.connect(dsn(), autocommit=True)
    except psycopg.OperationalError as exc:
        print(f"Cannot connect to the database: {exc}", file=sys.stderr)
        print("Is the container up?  docker compose up -d", file=sys.stderr)
        return 1

    with conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version text PRIMARY KEY,"
            "  applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {
            r[0]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }

        if status_only:
            for f in files:
                print(f"  [{'applied' if f.name in done else 'PENDING'}] {f.name}")
            return 0

        pending = [f for f in files if f.name not in done]
        if not pending:
            print("Database is up to date; nothing to apply.")
            return 0

        for f in pending:
            print(f"Applying {f.name} ...")
            body = f.read_text(encoding="utf-8")
            wrapped = (
                "BEGIN;\n"
                f"{body}\n"
                f"INSERT INTO schema_migrations (version) VALUES ('{f.name}');\n"
                "COMMIT;"
            )
            try:
                _run_script(conn, wrapped)
            except RuntimeError as exc:
                print(f"  FAILED (rolled back): {exc}", file=sys.stderr)
                return 1
            print("  ok")

        print(f"Applied {len(pending)} migration(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
