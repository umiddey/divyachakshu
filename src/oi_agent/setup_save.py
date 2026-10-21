"""Shared setup save: one writer for config + secrets used by `oi init` and `oi ui`."""

from __future__ import annotations

import os
import time
from pathlib import Path

from .config import Config, save_config
from .store import Store


class SetupSaveError(ValueError):
    """Raised when a setup payload fails validation or cannot be written."""


def _write_secret(path: Path, text: str) -> None:
    """Write a secrets file that is mode 0600 from its first byte.

    os.open applies 0600 at creation, so the token never sits in a file that is
    readable at umask permissions. fchmod tightens a pre-existing file before
    the token is written into it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)


def apply_setup(
    cfg: Config, discord_token: str, config_path: Path, secrets_path: Path
) -> None:
    """Validate and write the daemon config and bot-token secrets.

    The single shared implementation behind `oi init` and the browser UI so
    both produce identical files: config through ``save_config`` (validated,
    atomic), secrets with mode 0600, and the installed-at marker in the state
    store. The token is never echoed or logged.
    """
    try:
        save_config(cfg, config_path)
    except ValueError as exc:
        raise SetupSaveError(str(exc)) from exc
    if discord_token:
        _write_secret(secrets_path, f"{cfg.discord_token_env}={discord_token}\n")
    store = Store(Path(cfg.db_path).expanduser())
    try:
        store.set_installed_at(int(time.time()))
    finally:
        store.close()


def replace_discord_token(cfg: Config, token: str, secrets_path: Path) -> None:
    """Rewrite only the token line in the secrets file, keeping mode 0600."""
    env_name = cfg.discord_token_env
    kept: list[str] = []
    if secrets_path.exists():
        for raw in secrets_path.read_text(encoding="utf-8").splitlines():
            key = raw.split("=", 1)[0]
            if key != env_name:
                kept.append(raw)
    kept.append(f"{env_name}={token}")
    _write_secret(secrets_path, "\n".join(kept) + "\n")
