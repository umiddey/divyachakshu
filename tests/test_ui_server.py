"""P1: UI server security model, wizard/dashboard routing, `oi ui`, init offer."""

from __future__ import annotations

import urllib.parse

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient
from typer.testing import CliRunner
from ui_fakes import start_ui

import oi_agent.cli as cli_module
import oi_agent.ui.server as ui_server
from oi_agent.cli import app as cli_app
from oi_agent.ui.server import COOKIE_NAME

runner = CliRunner()
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ui(tmp_path):
    client, app = await start_ui(tmp_path / "config.toml", tmp_path / "secrets.env")
    yield client, app
    await client.close()


async def test_root_without_token_is_forbidden(ui: tuple[TestClient, object]):
    import aiohttp

    client, _ = ui
    async with aiohttp.ClientSession() as session:  # no cookies from the exchange
        async with session.get(client.make_url("/")) as resp:
            assert resp.status == 403


async def test_token_exchange_sets_httponly_strict_cookie(ui: tuple[TestClient, object]):
    client, app = ui
    import aiohttp

    url = client.make_url(f"/?t={app['state'].token}")
    async with aiohttp.ClientSession() as session:
        async with session.get(url, allow_redirects=False) as resp:
            assert resp.status == 200
            cookie = resp.headers["Set-Cookie"]
            assert f"{COOKIE_NAME}=" in cookie
            assert "HttpOnly" in cookie
            assert "SameSite=Strict" in cookie


async def test_wrong_token_is_forbidden(ui: tuple[TestClient, object]):
    client, _ = ui
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(client.make_url("/?t=not-the-token")) as resp:
            assert resp.status == 403


async def test_api_requires_cookie(ui: tuple[TestClient, object]):
    import aiohttp

    client, _ = ui
    async with aiohttp.ClientSession() as session:
        async with session.get(client.make_url("/api/state")) as resp:
            assert resp.status == 401


async def test_untrusted_host_header_rejected(ui: tuple[TestClient, object]):
    client, _ = ui
    response = await client.get("/api/state", headers={"Host": "rebind.example:1"})
    assert response.status == 403


async def test_foreign_origin_post_rejected(ui: tuple[TestClient, object]):
    client, _ = ui
    response = await client.post(
        "/api/pause", json={}, headers={"Origin": "http://evil.example:1"}
    )
    assert response.status == 403


async def test_same_origin_post_allowed(ui: tuple[TestClient, object]):
    client, app = ui
    origin = f"http://127.0.0.1:{app['state'].port}"
    response = await client.post("/api/pause", json={}, headers={"Origin": origin})
    assert response.status != 403  # auth/config errors would be different codes


async def test_state_mode_wizard_then_dashboard(ui: tuple[TestClient, object]):
    client, app = ui
    response = await client.get("/api/state")
    assert response.status == 200
    assert (await response.json())["mode"] == "wizard"

    app["state"].config_path.write_text('[placeholder]\n', encoding="utf-8")
    response = await client.get("/api/state")
    assert (await response.json())["mode"] == "dashboard"


async def test_static_requires_cookie(ui: tuple[TestClient, object]):
    import aiohttp

    client, _ = ui
    async with aiohttp.ClientSession() as session:
        async with session.get(client.make_url("/static/app.js")) as resp:
            assert resp.status == 401


# ------------------------------ CLI ------------------------------


def test_ui_command_refuses_foreign_host(tmp_path):
    result = runner.invoke(
        cli_app, ["ui", "--host", "0.0.0.0", "--no-browser",
                  "--config", str(tmp_path / "c.toml"), "--secrets", str(tmp_path / "s.env")]
    )
    assert result.exit_code == 1
    assert "refusing to bind" in result.output
    assert "127.0.0.1" in result.output


def test_run_ui_prints_tunnel_line_without_display(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 50000 10.0.0.2 22")
    monkeypatch.setattr(
        ui_server.web, "run_app",
        lambda app, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        ui_server.webbrowser, "open",
        lambda *_a, **_k: pytest.fail("browser must not open over SSH"),
    )
    code = ui_server.run_ui(tmp_path / "c.toml", tmp_path / "s.env")
    assert code == 0
    out = capsys.readouterr().out
    assert "OI setup UI: http://127.0.0.1:" in out
    assert "/?t=" in out
    assert "ssh -L " in out and "127.0.0.1:" in out
    token = urllib.parse.parse_qs(urllib.parse.urlparse(out.split(": ", 1)[1]).query)["t"][0]
    assert len(token.encode()) >= 32  # 32 random bytes, urlsafe-encoded


def test_run_ui_opens_browser_with_display(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DISPLAY", ":1")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    opened: list[str] = []
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        ui_server.web, "run_app",
        lambda app, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    code = ui_server.run_ui(tmp_path / "c.toml", tmp_path / "s.env")
    assert code == 0
    assert len(opened) == 1
    assert opened[0].startswith("http://127.0.0.1:")
    assert capsys.readouterr().out.startswith("OI setup UI: ")


def test_run_ui_honors_explicit_port(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        ui_server.web, "run_app",
        lambda app, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    code = ui_server.run_ui(tmp_path / "c.toml", tmp_path / "s.env", port=48125)
    assert code == 0
    assert "http://127.0.0.1:48125/?t=" in capsys.readouterr().out


def test_init_offer_enter_opens_ui(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_isatty", lambda: True)
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(cli_module, "run_ui", lambda cfg, sec: calls.append((cfg, sec)))
    result = runner.invoke(
        cli_app,
        ["init", "--config", str(tmp_path / "c.toml"), "--secrets", str(tmp_path / "s.env")],
        input="\n",
    )
    assert result.exit_code == 0
    assert "setup page" in result.output
    assert calls and calls[0][0] == tmp_path / "c.toml"


def test_init_offer_t_continues_terminal_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_isatty", lambda: True)
    calls: list[object] = []
    monkeypatch.setattr(cli_module, "run_ui", lambda *a, **k: calls.append(a))
    from oi_agent.opencode.bootstrap import OpenCodeBootstrapError

    def fail(*_a, **_k):
        raise OpenCodeBootstrapError("no opencode in this test")

    monkeypatch.setattr(cli_module, "_ensure_opencode", fail)
    result = runner.invoke(
        cli_app,
        ["init", "--config", str(tmp_path / "c.toml"), "--secrets", str(tmp_path / "s.env")],
        input="t\n",
    )
    assert not calls, "typing t must keep the terminal flow"
    assert result.exit_code == 1
    assert "no opencode in this test" in result.output


def test_init_offer_skipped_for_scripted_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_isatty", lambda: True)
    calls: list[object] = []
    monkeypatch.setattr(cli_module, "run_ui", lambda *a, **k: calls.append(a))
    from oi_agent.opencode.bootstrap import OpenCodeBootstrapError

    monkeypatch.setattr(
        cli_module, "_ensure_opencode",
        lambda *a, **k: (_ for _ in ()).throw(OpenCodeBootstrapError("flag flow")),
    )
    result = runner.invoke(
        cli_app,
        ["init", "--yes", "--config", str(tmp_path / "c.toml"), "--secrets", str(tmp_path / "s.env")],
        input="\n",
    )
    assert not calls
    assert "setup page" not in result.output


def test_init_offer_skipped_without_tty(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "_stdin_isatty", lambda: False)
    calls: list[object] = []
    monkeypatch.setattr(cli_module, "run_ui", lambda *a, **k: calls.append(a))
    from oi_agent.opencode.bootstrap import OpenCodeBootstrapError

    monkeypatch.setattr(
        cli_module, "_ensure_opencode",
        lambda *a, **k: (_ for _ in ()).throw(OpenCodeBootstrapError("non-tty flow")),
    )
    result = runner.invoke(
        cli_app,
        ["init", "--config", str(tmp_path / "c.toml"), "--secrets", str(tmp_path / "s.env")],
        input="\n",
    )
    assert not calls
    assert "setup page" not in result.output


def test_init_still_writes_config_and_secrets(tmp_path, monkeypatch):
    """Shared save: the terminal flow writes the same files as before the UI existed."""
    monkeypatch.setattr(cli_module, "_stdin_isatty", lambda: False)
    monkeypatch.setattr(
        cli_module, "_ensure_opencode", lambda *a, **k: "stepfun/gpt-test"
    )
    monkeypatch.setattr(cli_module.getpass, "getpass", lambda label: "tok-123")
    answers = iter([
        "OI_DISCORD_TOKEN",          # env var name
        "friendly",                  # personality
        "c", "1001",                 # watch type + channel
        str(tmp_path / "repo"),      # repo path
        "0",                         # bot user id
        "2",                         # cap
        "q",                         # done
        "n",                         # no daemon install
    ])
    monkeypatch.setattr(cli_module, "Prompt", _IterPrompt(answers))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    config = tmp_path / "c.toml"
    secrets = tmp_path / "s.env"
    result = runner.invoke(
        cli_app, ["init", "--config", str(config), "--secrets", str(secrets)], input="\n"
    )
    assert result.exit_code == 0, result.output
    assert config.exists()
    assert secrets.exists()
    assert oct(secrets.stat().st_mode & 0o777) == "0o600"
    body = secrets.read_text()
    assert "OI_DISCORD_TOKEN=tok-123" in body


class _IterPrompt:
    """Stand-in for rich.prompt.Prompt.ask that pops scripted answers."""

    def __init__(self, answers) -> None:
        self._answers = iter(answers)

    def ask(self, *_a, **_k):
        import pytest

        try:
            return next(self._answers)
        except StopIteration:
            pytest.fail("scripted init answers exhausted")
