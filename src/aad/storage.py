"""Postgres connection wrapper shared by the offline cache and the monitor store.

Both stores are written once, against SQLite's `?` placeholders and dict-like row
access (`sqlite3.Row` answers `row["col"]` and `dict(row)`). This wrapper lets the
exact same SQL and the exact same Python run against Postgres too — translating `?`
to `%s` and using psycopg's `dict_row` factory so a fetched row behaves the same way
— so neither store carries a second, parallel implementation of its own queries.

Why this exists at all: on a serverless host (Vercel, etc.) the filesystem outside
`/tmp` is read-only and `/tmp` itself does not survive a cold start, so a SQLite file
there silently resets on every cold start. A workshop with no connectivity still wants
SQLite; a deployment nobody can SSH into to look at a local file wants a real database.
"""

from __future__ import annotations

from typing import Any

from aad.errors import NotConfiguredError


class PgConnection:
    """Adapts a psycopg connection to the SQLite-shaped calls the stores make."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def execute(self, sql: str, params: Any = ()) -> Any:
        return self._raw.execute(sql.replace("?", "%s"), params)

    def executescript(self, sql: str) -> None:
        # psycopg's execute() does not run multiple statements in one call; the
        # schema strings are always `;`-separated CREATE TABLE/INDEX statements.
        with self._raw.cursor() as cur:
            for statement in filter(None, (s.strip() for s in sql.split(";"))):
                cur.execute(statement)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def connect_postgres(database_url: str | None, *, who: str) -> PgConnection:
    """Open a Postgres connection, or raise the same "not configured" error a
    missing commercial-provider credential raises — a durable store that silently
    isn't there is exactly the kind of gap this system refuses to paper over.
    """
    if not database_url:
        raise NotConfiguredError(
            who, "AAD_STORAGE_BACKEND=postgres but no DATABASE_URL is set"
        )
    import psycopg
    from psycopg.rows import dict_row

    return PgConnection(psycopg.connect(database_url, row_factory=dict_row))
