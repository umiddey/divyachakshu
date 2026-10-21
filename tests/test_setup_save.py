"""Secrets files are created 0600 at creation, never readable at umask mode."""

from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import oi_agent.setup_save as setup_save


def test_token_file_is_created_with_0600_not_umask_mode(tmp_path, monkeypatch):
    created_modes: list[int] = []
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *args, **kwargs):
        created_modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy_open)
    cfg = SimpleNamespace(discord_token_env="OI_DISCORD_TOKEN")
    secrets = tmp_path / "secrets.env"

    old_umask = os.umask(0o022)  # a normal umask would leave a new file 0644
    try:
        setup_save.replace_discord_token(cfg, "tok-123", secrets)
    finally:
        os.umask(old_umask)

    assert created_modes == [0o600]
    assert secrets.read_text(encoding="utf-8") == "OI_DISCORD_TOKEN=tok-123\n"
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600
