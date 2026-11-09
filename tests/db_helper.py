"""PostgreSQL for the tests.

The settings come from the same DB_* environment variables as the service.
DB_NAME must end with "_test": every test drops the videos table.
Without a database the tests that need it are skipped; with REQUIRE_DB=1 (CI)
they fail instead, so a broken database setup cannot hide.

A local test database (see README, "Tests"):
    docker run -d --name videos-test-db -p 127.0.0.1:5433:5432 -e POSTGRES_DB=videos_test
        -e POSTGRES_USER=video_user -e POSTGRES_PASSWORD=test_only postgres:16
"""

import os
import unittest

import psycopg2
import psycopg2.extras

import web_service


def _find_settings():
    try:
        settings = web_service.db_settings()
    except RuntimeError as exc:
        return None, str(exc)
    if not settings["dbname"].endswith("_test"):
        return None, f"DB_NAME={settings['dbname']} does not end with _test (the tests drop the videos table)"
    try:
        psycopg2.connect(connect_timeout=3, **settings).close()
    except psycopg2.OperationalError as exc:
        return None, f"cannot connect to {web_service.describe(settings)}: {str(exc).strip()}"
    return settings, None


SETTINGS, REASON = _find_settings()


def needs_db(cls):
    """Class decorator: skip without a test database, fail if REQUIRE_DB=1."""
    if SETTINGS is not None:
        return cls
    if os.environ.get("REQUIRE_DB") == "1":
        def setUpClass(klass):
            raise RuntimeError(f"REQUIRE_DB=1, but there is no test database: {REASON}")
        cls.setUpClass = classmethod(setUpClass)
        return cls
    return unittest.skip(f"no test database: {REASON}")(cls)


def sql(query, args=(), dicts=False):
    """Runs one statement in its own connection, commits, returns the rows (or [])."""
    factory = psycopg2.extras.RealDictCursor if dicts else None
    conn = psycopg2.connect(**SETTINGS)
    try:
        with conn, conn.cursor(cursor_factory=factory) as cur:
            cur.execute(query, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def reset_db():
    """No table, like a fresh database: create_app() must create it."""
    sql("DROP TABLE IF EXISTS videos")
