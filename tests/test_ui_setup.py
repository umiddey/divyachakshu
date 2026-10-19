"""P2: setup wizard endpoints — OpenCode install/login/models, Discord, repos, save, service."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest
import pytest_asyncio
from ui_fakes import (
    build_fake_discord_app,
    build_fake_opencode_app,
    openapi_paths,
    start_fake,
    start_ui,
)

import oi_agent.ui.server as ui_server
from oi_agent.config import load_config

pytestmark = pytest.mark.asyncio

FAKE_TOKEN = "your-fake-bot-token"


@pytest_asyncio.fixture
async def wizard(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    oc_fake = await start_fake(build_fake_opencode_app())
    discord_fake = await start_fake(build_fake_discord_app())
    monkeypatch.setenv("OI_DISCORD_API_BASE", str(discord_fake.make_url("")).rstrip("/"))
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    client, app = await start_ui(tmp_path / "config.toml", tmp_path / "secrets.env")
    monkeypatch.setattr(
        ui_server, "start_opencode_serve",
        lambda binary, paths: (None, str(oc_fake.make_url("")).rstrip("/")),
    )
    try:
        yield SimpleNamespace(
            client=client, app=app, oc_state=oc_fake.app["oauth_state"],
            oc_server=oc_fake, discord_state=discord_fake.app["state"],
            repo=repo, home=home, tmp=tmp_path,
        )
    finally:
        await client.close()
        await oc_fake.close()
        await discord_fake.close()


def fake_opencode_on_path(tmp_path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "opencode"
    fake.write_text('#!/bin/sh\necho "opencode 2.0.5 (fake)"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")


# ---------------- fixture integrity ----------------


async def test_openapi_fixture_covers_all_fake_serve_routes():
    """The committed 2.0.5 openapi fixture is the source the fakes mirror."""
    paths = openapi_paths()
    for required in (
        "/api/integration",
        "/api/integration/{integrationID}/connect/key",
        "/api/integration/{integrationID}/connect/oauth",
        "/api/integration/{integrationID}/connect/oauth/{attemptID}",
        "/api/integration/{integrationID}/connect/oauth/{attemptID}/complete",
        "/api/provider",
        "/api/model",
    ):
        assert required in paths, required


# ---------------- OpenCode wizard ----------------


async def test_opencode_status_reports_version(wizard, monkeypatch):
    fake_opencode_on_path(wizard.tmp, monkeypatch)
    response = await wizard.client.get("/api/opencode/status")
    data = await response.json()
    assert data["installed"] is True
    assert data["version"] == "2.0.5"
    assert data["authenticated"] is False


async def test_opencode_status_shows_integration_display_name(wizard, monkeypatch):
    """Connected providers show Integration.Info.name, not the npm package id."""
    fake_opencode_on_path(wizard.tmp, monkeypatch)
    monkeypatch.setattr(ui_server.oc, "authenticated", lambda paths: True)
    monkeypatch.setattr(
        ui_server.oc, "credential_integrations",
        lambda paths: frozenset({"npm:@ai-sdk/openai-compatible"}),
    )
    start = await wizard.client.post("/api/opencode/login/start", json={})
    assert (await start.json())["ok"] is True  # private serve serves the names
    response = await wizard.client.get("/api/opencode/status")
    data = await response.json()
    assert data["authenticated"] is True
    assert data["integrations"] == [
        {"id": "npm:@ai-sdk/openai-compatible", "name": "OpenAI-compatible"}
    ]


async def test_opencode_status_falls_back_to_integration_id(wizard, monkeypatch):
    """Without a live serve there is no name source: show the id as-is."""
    fake_opencode_on_path(wizard.tmp, monkeypatch)
    monkeypatch.setattr(ui_server.oc, "authenticated", lambda paths: True)
    monkeypatch.setattr(
        ui_server.oc, "credential_integrations",
        lambda paths: frozenset({"npm:@ai-sdk/openai-compatible"}),
    )
    response = await wizard.client.get("/api/opencode/status")
    data = await response.json()
    assert data["integrations"] == [
        {"id": "npm:@ai-sdk/openai-compatible", "name": "npm:@ai-sdk/openai-compatible"}
    ]


async def test_opencode_install_runs_shared_installer(wizard, monkeypatch):
    fake_opencode_on_path(wizard.tmp, monkeypatch)
    calls: list[object] = []
    monkeypatch.setattr(
        ui_server.oc, "install_opencode", lambda paths: calls.append(paths) or "installed"
    )
    response = await wizard.client.post("/api/opencode/install", json={})
    data = await response.json()
    assert data["ok"] is True
    assert data["version"] == "2.0.5"
    assert len(calls) == 1


async def test_login_start_integrations_and_stop(wizard):
    client = wizard.client
    start = await client.post("/api/opencode/login/start", json={})
    assert (await start.json())["ok"] is True
    listing = await client.get("/api/opencode/login/integrations")
    integrations = (await listing.json())["integrations"]
    assert integrations and integrations[0]["id"] == "npm:@ai-sdk/openai-compatible"
    stop = await client.post("/api/opencode/login/stop", json={})
    assert (await stop.json())["ok"] is True


async def test_connect_key_flow(wizard):
    client = wizard.client
    await client.post("/api/opencode/login/start", json={})
    response = await client.post(
        "/api/opencode/login/key",
        json={"integration": "npm:@ai-sdk/openai-compatible", "key": "k-test-1"},
    )
    assert (await response.json())["ok"] is True
    assert wizard.oc_state["keys"] == ["k-test-1"]


async def test_oauth_flow_poll_and_complete(wizard):
    client = wizard.client
    await client.post("/api/opencode/login/start", json={})
    start = await client.post(
        "/api/opencode/login/oauth",
        json={"integration": "npm:@ai-sdk/openai-compatible", "method_id": "oauth"},
    )
    data = await start.json()
    assert data["ok"] is True
    assert data["attempt_id"] == "attempt-1"
    assert data["url"] == "https://example.invalid/oauth/signin"

    status = await client.get("/api/opencode/login/oauth/attempt-1")
    assert (await status.json())["done"] is False

    wizard.oc_state["oauth"]["attempt-1"]["status"] = "ok"
    status = await client.get("/api/opencode/login/oauth/attempt-1")
    assert (await status.json())["done"] is True

    complete = await client.post("/api/opencode/login/oauth/attempt-1/complete", json={})
    assert (await complete.json())["ok"] is True


async def test_models_dropdown_lists_provider_models(wizard):
    client = wizard.client
    await client.post("/api/opencode/login/start", json={})
    response = await client.get("/api/opencode/models")
    models = (await response.json())["models"]
    assert "stepfun/gpt-test" in models


async def test_login_start_without_opencode_binary_fails_cleanly(wizard, monkeypatch):
    empty_bin = wizard.tmp / "emptybin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))  # no opencode anywhere
    response = await wizard.client.post("/api/opencode/login/start", json={})
    data = await response.json()
    assert data["ok"] is False
    assert "opencode" in data["error"].lower()


# ---------------- Discord wizard ----------------


async def test_discord_validate_ok_with_intent(wizard):
    response = await wizard.client.post(
        "/api/discord/validate", json={"token": FAKE_TOKEN}
    )
    data = await response.json()
    assert data["ok"] is True
    assert data["application"]["id"] == "9001"
    assert data["message_content_enabled"] is True
    assert "client_id=9001" in data["invite_url"]
    assert "permissions=274877975552" in data["invite_url"]
    assert data["hint"] is None


async def test_discord_validate_flags_missing_intent(wizard):
    wizard.discord_state["flags"] = 0
    response = await wizard.client.post(
        "/api/discord/validate", json={"token": FAKE_TOKEN}
    )
    data = await response.json()
    assert data["message_content_enabled"] is False
    assert "MESSAGE CONTENT INTENT" in data["hint"]
    assert FAKE_TOKEN not in (await response.text())


async def test_discord_rejects_bad_token(wizard):
    response = await wizard.client.post(
        "/api/discord/validate", json={"token": "wrong"}
    )
    data = await response.json()
    assert data["ok"] is False
    assert "401" in data["error"]


async def test_discord_guilds_and_channels(wizard):
    client = wizard.client
    guilds = await (await client.post("/api/discord/guilds", json={"token": FAKE_TOKEN})).json()
    assert [g["id"] for g in guilds["guilds"]] == ["100", "200"]
    channels = await (
        await client.post("/api/discord/channels", json={"token": FAKE_TOKEN, "guild_id": "100"})
    ).json()
    names = [c["name"] for c in channels["channels"]]
    assert names == ["general", "help", "announcements"]  # voice-only (type 2) filtered
    request_paths = [r["path"] for r in wizard.discord_state["requests"]]
    assert all(FAKE_TOKEN not in path for path in request_paths)  # token stays in bodies


# ---------------- Repos wizard ----------------


async def test_repos_scan_depth_and_skips(wizard):
    home = wizard.home
    (home / "alpha" / ".git").mkdir(parents=True)
    (home / "nested" / "beta" / ".git").mkdir(parents=True)
    (home / "nested" / "gamma" / "delta" / ".git").mkdir(parents=True)
    (home / "deeper" / "l1" / "l2" / "l3" / ".git").mkdir(parents=True)  # depth 4
    (home / ".hidden" / ".git").mkdir(parents=True)
    (home / "node_modules" / ".git").mkdir(parents=True)
    response = await wizard.client.post("/api/repos/scan", json={"root": str(home)})
    repos = (await response.json())["repos"]
    assert repos == [
        str((home / "alpha").resolve()),
        str((home / "nested" / "beta").resolve()),
        str((home / "nested" / "gamma" / "delta").resolve()),
    ]


async def test_repos_validate(wizard):
    bad = await (await wizard.client.post(
        "/api/repos/validate", json={"path": str(wizard.tmp)}
    )).json()
    assert bad["ok"] is False
    assert "not a git clone" in bad["error"]
    good = await (await wizard.client.post(
        "/api/repos/validate", json={"path": str(wizard.repo)}
    )).json()
    assert good == {"ok": True, "path": str(wizard.repo.resolve())}


# ---------------- Save + service ----------------


async def test_save_writes_config_and_secrets_exactly(wizard):
    client = wizard.client
    payload = {
        "config": {
            "discord_token_env": "OI_DISCORD_TOKEN",
            "personality": "quiet teammate",
            "environment_mode": "workstation",
            "opencode_model": "stepfun/gpt-test",
            "watches": [{
                "guild_id": 100,
                "repos": [str(wizard.repo)],
                "post_hourly_cap": 2,
                "allowed_channels": [1001, 1002],
            }],
        },
        "discord_token": "your-real-token-42",
    }
    response = await client.post("/api/save", json=payload)
    data = await response.json()
    assert data["ok"] is True, data

    cfg = load_config(wizard.app["state"].config_path)
    assert cfg.personality == "quiet teammate"
    assert cfg.environment_mode == "workstation"
    assert cfg.opencode_model == "stepfun/gpt-test"
    assert len(cfg.watches) == 1
    watch = cfg.watches[0]
    assert watch.guild_id == 100
    assert watch.repos == [str(wizard.repo)]
    assert watch.post_hourly_cap == 2
    assert watch.allowed_channels == [1001, 1002]

    secrets_path = wizard.app["state"].secrets_path
    assert secrets_path.read_text(encoding="utf-8") == "OI_DISCORD_TOKEN=your-real-token-42\n"
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    assert "your-real-token-42" not in await response.text()

    # Every wizard API answers JSON; the token never appears in any URL.
    for request in wizard.discord_state["requests"]:
        assert "your-real-token-42" not in request["path"]


async def test_save_invalid_watch_surfaces_save_config_error(wizard):
    response = await wizard.client.post("/api/save", json={
        "config": {"watches": [{"guild_id": 100}]},  # no repos
        "discord_token": "",
    })
    data = await response.json()
    assert data["ok"] is False
    assert "repo" in data["error"].lower()
    assert not wizard.app["state"].config_path.exists()


async def test_save_unknown_key_rejected(wizard):
    response = await wizard.client.post("/api/save", json={
        "config": {"no_such_key": 1, "watches": []},
    })
    data = await response.json()
    assert data["ok"] is False
    assert "no_such_key" in data["error"]


async def test_save_without_watches_rejected(wizard):
    response = await wizard.client.post("/api/save", json={
        "config": {"personality": "x"}, "discord_token": "",
    })
    data = await response.json()
    assert data["ok"] is False
    assert "watch" in data["error"]


async def test_service_install_uses_seam(wizard):
    client = wizard.client
    saved = await client.post("/api/save", json={
        "config": {
            "environment_mode": "server",
            "watches": [{"channel_id": 1001, "repos": [str(wizard.repo)]}],
        },
        "discord_token": "",
    })
    assert (await saved.json())["ok"] is True
    calls: list[dict] = []

    def fake_install(cfg, **kwargs):
        calls.append({"cfg": cfg, **kwargs})
        return "rendered-unit"

    monkey_install = pytest.MonkeyPatch()
    monkey_install.setattr(ui_server, "install_daemon", fake_install)
    try:
        response = await client.post("/api/service/install", json={})
    finally:
        monkey_install.undo()
    data = await response.json()
    assert data["ok"] is True
    assert data["user_mode"] is True  # tests never run as root
    assert len(calls) == 1
    assert calls[0]["cfg"].environment_mode == "server"
    assert calls[0]["config_path"] == wizard.app["state"].config_path
    assert calls[0]["secrets_path"] == wizard.app["state"].secrets_path
