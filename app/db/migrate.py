"""Apply migrations/*.sql in order, once each, as the migrator role.

The migrator is the only role allowed to alter tables. Plain SQL files and a tiny runner instead of
Alembic: a dozen files, no ORM, easy to explain to a client.
Usage: `make migrate` (or `uv run python -m app.db.migrate`).
"""

import sys
from pathlib import Path

import psycopg

from app.settings import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


def apply_migrations(database_url: str | None = None) -> list[str]:
    """Return the names of the migrations applied by this call (empty if already up to date)."""
    url = database_url or get_settings().migrator_database_url
    applied_now: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in done:
                continue
            # One transaction per file: a failing migration leaves nothing half-applied.
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    applied = apply_migrations()
    print("applied:", ", ".join(applied) if applied else "nothing (already up to date)")
    sys.exit(0)
