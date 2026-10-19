"""P3: dashboard — status, pause/resume, restart, logs, settings editor, token replace."""

from __future__ import annotations

import dataclasses
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from ui_fakes import start_ui

import oi_agent.ui.server as ui_server
from oi_agent.config import Config, WatchTarget, load_config, save_config
from oi_agent.store import Store

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def dash(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.env"
    cfg = Config(
        personality="team-mate",
        environment_mode="server",
        opencode_model="stepfun/gpt-test",
        write_dirs=["docs"],
        watches=[WatchTarget(channel_id=1001, repos=[str(repo)], post_hourly_cap=2)],
    )
    save_config(cfg, config_path)
    secrets_path.write_text("OI_DISCORD_TOKEN=old-token\n", encoding="utf-8")
    secrets_path.chmod(0o600)
    # State store pre-seeded: paused, one recorded post.
    db_path = Path(home / ".local/state/oi/state.db")
    store = Store(db_path)
    try:
        store.set_paused(True)
        store._conn.execute("INSERT INTO posts(ts, channel_id) VALUES(1700000000, 1001)")
        store._conn.commit()
    finally:
        store.close()
    # Never probe the real service manager in tests.
    monkeypatch.setattr(ui_server, "service_running", lambda: False)
    client, app = await start_ui(config_path, secrets_path)
    try:
        yield SimpleNamespace(
            client=client, app=app, config_path=config_path,
            secrets_path=secrets_path, repo=repo, home=home, tmp=tmp_path,
        )
    finally:
        await client.close()


async def read_store(dash):
    cfg = load_config(dash.config_path)
    store = Store(Path(cfg.db_path).expanduser())
    try:
        return store.is_paused()
    finally:
        store.close()


async def test_status_reports_paused_and_last_activity(dash):
    response = await dash.client.get("/api/status")
    data = await response.json()
    assert data["ok"] is True
    assert data["paused"] is True
    assert data["last_activity"] == 1700000000
    assert data["service_running"] is False


async def test_pause_resume_roundtrip(dash):
    resume = await (await dash.client.post("/api/resume", json={})).json()
    assert resume == {"ok": True, "paused": False}
    assert await read_store(dash) is False
    pause = await (await dash.client.post("/api/pause", json={})).json()
    assert pause == {"ok": True, "paused": True}
    assert await read_store(dash) is True


async def test_restart_uses_lifecycle_seam(dash):
    calls: list[tuple] = []

    def fake_lifecycle(action, *, service_name, user_mode):
        calls.append((action, service_name, user_mode))

    import pytest

    patch = pytest.MonkeyPatch()
    patch.setattr(ui_server, "daemon_lifecycle", fake_lifecycle)
    try:
        response = await dash.client.post("/api/restart", json={})
    finally:
        patch.undo()
    assert (await response.json())["ok"] is True
    assert calls == [("restart", "oi", True)]  # user mode: tests never run as root


async def test_restart_failure_surfaces_error(dash):
    def boom(action, *, service_name, user_mode):
        raise RuntimeError("systemctl --user command failed")

    import pytest

    patch = pytest.MonkeyPatch()
    patch.setattr(ui_server, "daemon_lifecycle", boom)
    try:
        response = await dash.client.post("/api/restart", json={})
    finally:
        patch.undo()
    data = await response.json()
    assert data["ok"] is False
    assert "systemctl --user command failed" in data["error"]


async def test_logs_last_200_lines(dash):
    import pytest

    patch = pytest.MonkeyPatch()
    patch.setattr(
        ui_server, "read_daemon_logs",
        lambda lines: "\n".join(f"line-{i}" for i in range(lines)),
    )
    try:
        response = await dash.client.get("/api/logs?lines=200")
    finally:
        patch.undo()
    data = await response.json()
    assert data["ok"] is True
    assert len(data["lines"]) == 200
    assert data["lines"][0] == "line-0"
    assert "line-0\nline-1" in "\n".join(data["lines"][:2])


async def test_logs_failure_surfaces_error(dash):
    import pytest

    patch = pytest.MonkeyPatch()
    patch.setattr(
        ui_server, "read_daemon_logs",
        lambda lines: (_ for _ in ()).throw(RuntimeError("journalctl failed")),
    )
    try:
        response = await dash.client.get("/api/logs")
    finally:
        patch.undo()
    data = await response.json()
    assert data["ok"] is False
    assert "journalctl failed" in data["error"]


async def test_settings_expose_every_config_field(dash):
    response = await dash.client.get("/api/settings")
    data = await response.json()
    assert data["ok"] is True
    field_names = {f.name for f in dataclasses.fields(Config)}
    assert set(data["config"]) == field_names  # every field, nothing secret
    assert data["config"]["personality"] == "team-mate"
    assert data["config"]["watches"][0]["channel_id"] == 1001
    assert data["config"]["watches"][0]["post_hourly_cap"] == 2


async def test_settings_save_roundtrips_every_field(dash):
    get = await (await dash.client.get("/api/settings")).json()
    cfg = get["config"]
    cfg["personality"] = "edited personality"
    cfg["max_reply_chars"] = 1500
    cfg["environment_mode"] = "workstation"
    cfg["opencode_model"] = "other/model"
    cfg["write_dirs"] = ["docs2"]
    cfg["watches"] = [
        {"guild_id": 100, "repos": [str(dash.repo)], "post_hourly_cap": 5,
         "ignored_channels": [1003], "allowed_channels": [1001]},
        {"channel_id": 2002, "repos": [str(dash.repo)], "post_hourly_cap": 1},
    ]
    saved = await (await dash.client.post("/api/settings", json={"config": cfg})).json()
    assert saved == {"ok": True, "restart_available": True}

    reloaded = load_config(dash.config_path)
    assert reloaded.personality == "edited personality"
    assert reloaded.max_reply_chars == 1500
    assert reloaded.environment_mode == "workstation"
    assert reloaded.opencode_model == "other/model"
    assert reloaded.write_dirs == ["docs2"]
    assert [w.guild_id for w in reloaded.watches] == [100, 0]
    assert reloaded.watches[0].ignored_channels == [1003]
    assert reloaded.watches[0].allowed_channels == [1001]
    # Secrets untouched by a settings save.
    assert dash.secrets_path.read_text(encoding="utf-8") == "OI_DISCORD_TOKEN=old-token\n"
    assert stat.S_IMODE(dash.secrets_path.stat().st_mode) == 0o600


async def test_settings_invalid_value_shows_save_config_error(dash):
    get = await (await dash.client.get("/api/settings")).json()
    cfg = get["config"]
    cfg["reply_delivery"] = "bogus-mode"
    saved = await (await dash.client.post("/api/settings", json={"config": cfg})).json()
    assert saved["ok"] is False
    assert "reply_delivery" in saved["error"]
    assert load_config(dash.config_path).reply_delivery == "chunked"


async def test_settings_unknown_key_rejected(dash):
    get = await (await dash.client.get("/api/settings")).json()
    cfg = get["config"]
    cfg["mystery"] = 1
    saved = await (await dash.client.post("/api/settings", json={"config": cfg})).json()
    assert saved["ok"] is False
    assert "mystery" in saved["error"]


async def test_token_replace_writes_0600_and_never_echoes(dash):
    response = await dash.client.post("/api/secrets/token", json={"token": "your-new-token"})
    data = await response.json()
    assert data == {"ok": True}
    assert stat.S_IMODE(dash.secrets_path.stat().st_mode) == 0o600
    body = dash.secrets_path.read_text(encoding="utf-8")
    assert "OI_DISCORD_TOKEN=your-new-token\n" in body
    assert "your-new-token" not in await response.text()
    settings = await (await dash.client.get("/api/settings")).json()
    assert "your-new-token" not in str(settings)


async def test_token_missing_rejected(dash):
    response = await dash.client.post("/api/secrets/token", json={"token": ""})
    assert (await response.json())["ok"] is False
