"""Smoke support: fake `opencode serve` process + native-store credential sink.

`serve` mode is exec'd by the fake `opencode` shell wrapper the smoke puts on
PATH; it parses `--hostname/--port` and serves the same fake app the tests use.
Credentials are written into OpenCode's native store under the sandbox XDG
data home, mirroring what the real serve does on a completed connect.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _data_home() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def native_credential_sink(integration_id: str, key: str) -> None:
    """Store one credential row like `opencode serve` would (sandbox only)."""
    if not integration_id:
        return
    database = _data_home() / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS credential ("
            "id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
            "connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        now = int(time.time())
        connection.execute(
            "INSERT OR REPLACE INTO credential (id, integration_id, label, value, "
            "connector_id, method_id, active, time_created, time_updated) "
            "VALUES (?, ?, ?, ?, NULL, NULL, 1, ?, ?)",
            (f"cred-smoke-{integration_id}", integration_id, "API key",
             f"smoke-{key}", now, now),
        )
        connection.commit()
    finally:
        connection.close()


def serve(argv: list[str]) -> None:
    port = 0
    for index, flag in enumerate(argv):
        if flag == "--port" and index + 1 < len(argv):
            port = int(argv[index + 1])
    sys.path.insert(0, str(REPO / "tests"))
    from aiohttp import web

    from ui_fakes import build_fake_opencode_app

    web.run_app(
        build_fake_opencode_app(credential_sink=native_credential_sink),
        host="127.0.0.1", port=port, print=None, access_log=None,
    )


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve(sys.argv[2:])
    else:
        raise SystemExit("usage: ui_smoke_fakes.py serve [--hostname H] [--port N]")
