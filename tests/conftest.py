"""Shared fixtures for OI tests."""

import sqlite3

import pytest


@pytest.fixture
def seed_credential_db():
    """Seed an isolated native store with provider credential rows.

    Mirrors the real credential table shape (native.CREDENTIAL_COLUMNS)
    without touching any real secret material. Call once per row; the table
    is created on first use.
    """

    def _seed(data_home, integration_id, active=1):
        db = data_home / "opencode" / "opencode.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS credential ("
                "id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
                "connector_id TEXT, method_id TEXT, active INTEGER, "
                "time_created INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO credential (id, integration_id, label, value, active) "
                "VALUES (?, ?, 'API key', 'test-fixture-not-a-secret', ?)",
                (f"cred_test_{integration_id}_{active}", integration_id, active),
            )
            conn.commit()
        finally:
            conn.close()
        return db

    return _seed
