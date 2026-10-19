"""Shared fixtures for OI tests."""

import os
import sqlite3

import pytest


@pytest.fixture(autouse=True)
def fake_opencode_on_path(tmp_path, monkeypatch):
    """Put a fake `opencode` executable first on PATH for every test.

    Config validation (``config._resolve_binary``) and OpenCode bootstrap
    discovery (``bootstrap.resolve_binary``) resolve the bare name
    ``opencode`` through PATH, so without this fixture every test that
    persists a config needs a real OpenCode install, and on machines that
    have one the real binary would be executed. The fake answers
    ``--version`` with a supported native v2 release and ``models`` with a
    canned listing, so the suite runs on a machine without OpenCode and
    never executes a real one.
    """
    bin_dir = tmp_path / "fake-opencode-bin"
    bin_dir.mkdir()
    fake = bin_dir / "opencode"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  --version) echo 'opencode 2.0.5' ;;\n"
        "  models) echo 'openai/gpt-4o-mini' ;;\n"
        "  *) : ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))


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
