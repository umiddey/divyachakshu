"""Smoke wrapper: the real UI server with a fake Discord API and a recorded
install_daemon. Writes ui.json {port, token} once the app is built so the
driver can open the first URL.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))


def main() -> None:
    smoke_dir = Path(os.environ["SMOKE_DIR"])
    from aiohttp.test_utils import TestServer
    from ui_fakes import build_fake_discord_app

    from oi_agent import config as oi_config
    from oi_agent.ui import server as ui_server

    # The fake Discord must live on the SAME event loop as the UI server:
    # run_ui -> web.run_app builds its own loop, and a TestServer started
    # under asyncio.run dies with that loop (its socket closes with it).
    discord = TestServer(build_fake_discord_app())

    def fake_install(cfg, **kwargs):
        (smoke_dir / "install_daemon.json").write_text(json.dumps({
            "user_mode": kwargs.get("user_mode"),
            "config_path": str(kwargs.get("config_path")),
            "secrets_path": str(kwargs.get("secrets_path")),
        }))
        return "smoke-fake unit"

    ui_server.install_daemon = fake_install

    real_create = ui_server.create_app

    def create_app(config_path, secrets_path, port):
        app = real_create(config_path, secrets_path, port)

        async def start_fake_discord(_app):
            await discord.start_server()
            os.environ["OI_DISCORD_API_BASE"] = str(discord.make_url("")).rstrip("/")

        async def stop_fake_discord(_app):
            await discord.close()

        app.on_startup.append(start_fake_discord)
        app.on_cleanup.append(stop_fake_discord)
        (smoke_dir / "ui.json").write_text(json.dumps({
            "port": port, "token": app["state"].token,
        }))
        return app

    ui_server.create_app = create_app
    ui_server.run_ui(
        oi_config.DEFAULT_CONFIG_PATH, oi_config.DEFAULT_SECRETS_PATH,
        open_browser=False,
    )


if __name__ == "__main__":
    main()
