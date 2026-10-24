"""OI keeps its private npm cache bounded by itself.

Covers:
- `prune_private_npm` removes `home/.npm/_cacache` and stale `home/.npm/_npx/*`
  entries while keeping the entry holding the binary OI currently runs.
- Bytes freed are returned and logged as one bounded line.
- Symlink escapes out of the private npm root are refused.
- The watcher prunes at startup and then at most once per day.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from oi_agent.config import Config, WatchTarget
from oi_agent.opencode import bootstrap
from oi_agent.opencode.paths import OpenCodePaths, resolve_opencode_paths
from oi_agent.opencode.storage import prune_private_npm
from oi_agent.store import Store
from oi_agent.watch import discord_client
from oi_agent.watch.discord_client import OIWatcher

STORAGE_LOGGER = "oi_agent.opencode.storage"


def _paths(tmp_path: Path) -> OpenCodePaths:
    return resolve_opencode_paths(tmp_path / "svc")


def _fill(root: Path, size: int) -> int:
    root.mkdir(parents=True, exist_ok=True)
    (root / "payload").write_bytes(b"x" * size)
    return size


def _watcher(tmp_path: Path) -> OIWatcher:
    cfg = Config()
    cfg.watches = [WatchTarget(111, str(tmp_path / "repo"), 99, 10)]
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


def test_prune_removes_cacache_and_stale_npx_keeps_binary_entry(tmp_path, caplog):
    paths = _paths(tmp_path)
    home = paths.home
    _fill(home / ".npm" / "_npx" / "aaa", 100)
    stale = _fill(home / ".npm" / "_npx" / "bbb", 200)
    cache = _fill(home / ".npm" / "_cacache", 3000)
    keep = home / ".npm" / "_npx" / "aaa" / "payload"
    with caplog.at_level(logging.INFO, logger=STORAGE_LOGGER):
        freed = prune_private_npm(paths, keep)
    assert freed == stale + cache
    assert (home / ".npm" / "_npx" / "aaa").is_dir()
    assert not (home / ".npm" / "_cacache").exists()
    assert not (home / ".npm" / "_npx" / "bbb").exists()
    assert "storage prune freed_bytes=" in caplog.text
    assert "removed=2" in caplog.text


def test_prune_without_keep_clears_every_npx_entry(tmp_path):
    paths = _paths(tmp_path)
    home = paths.home
    _fill(home / ".npm" / "_npx" / "aaa", 100)
    _fill(home / ".npm" / "_npx" / "bbb", 200)
    _fill(home / ".npm" / "_cacache", 3000)
    freed = prune_private_npm(paths, None)
    assert freed == 3300
    assert not (home / ".npm" / "_npx" / "aaa").exists()
    assert not (home / ".npm" / "_npx" / "bbb").exists()
    assert not (home / ".npm" / "_cacache").exists()


def test_prune_refuses_symlink_escape_out_of_private_root(tmp_path, caplog):
    paths = _paths(tmp_path)
    home = paths.home
    victim = tmp_path / "victim"
    _fill(victim, 50)
    (home / ".npm").mkdir(parents=True)
    (home / ".npm" / "_cacache").symlink_to(victim, target_is_directory=True)
    (home / ".npm" / "_npx").mkdir()
    escape = home / ".npm" / "_npx" / "esc"
    escape.symlink_to(victim, target_is_directory=True)
    kept = _fill(home / ".npm" / "_npx" / "ok", 10)
    with caplog.at_level(logging.WARNING, logger=STORAGE_LOGGER):
        freed = prune_private_npm(paths, None)
    assert freed == kept
    assert (victim / "payload").exists()
    assert escape.is_symlink()
    assert (home / ".npm" / "_cacache").is_symlink()
    assert "refused" in caplog.text


def test_prune_missing_private_root_is_a_noop(tmp_path):
    freed = prune_private_npm(_paths(tmp_path), None)
    assert freed == 0


def test_watcher_prune_keeps_resolved_binary_entry(tmp_path, monkeypatch):
    watcher = _watcher(tmp_path)
    paths = _paths(tmp_path)
    home = paths.home
    _fill(home / ".npm" / "_npx" / "aaa", 100)
    _fill(home / ".npm" / "_npx" / "bbb", 200)
    _fill(home / ".npm" / "_cacache", 3000)
    binary = home / ".npm" / "_npx" / "aaa" / "payload"
    monkeypatch.setattr(discord_client, "resolve_opencode_paths", lambda: paths)
    monkeypatch.setattr(
        discord_client, "resolve_runtime_binary", lambda _b, _p: str(binary)
    )
    watcher._prune_npm_cache()
    assert binary.exists()
    assert not (home / ".npm" / "_npx" / "bbb").exists()
    assert not (home / ".npm" / "_cacache").exists()


def test_watcher_prune_survives_binary_resolution_failure(tmp_path, monkeypatch):
    watcher = _watcher(tmp_path)
    paths = _paths(tmp_path)
    home = paths.home
    _fill(home / ".npm" / "_npx" / "aaa", 100)
    _fill(home / ".npm" / "_cacache", 3000)

    def _boom(_binary, _paths):
        raise bootstrap.OpenCodeBootstrapError("unresolvable")

    monkeypatch.setattr(discord_client, "resolve_opencode_paths", lambda: paths)
    monkeypatch.setattr(discord_client, "resolve_runtime_binary", _boom)
    watcher._prune_npm_cache()
    assert not (home / ".npm" / "_npx" / "aaa").exists()
    assert not (home / ".npm" / "_cacache").exists()


@pytest.mark.asyncio
async def test_storage_prune_loop_runs_at_start_then_once_per_interval(tmp_path, monkeypatch):
    watcher = _watcher(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(watcher, "_prune_npm_cache", lambda: calls.append(1))
    monkeypatch.setattr(discord_client, "STORAGE_PRUNE_INTERVAL_SECONDS", 0.05)
    task = asyncio.create_task(watcher._storage_prune_loop())
    await asyncio.sleep(0.16)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_on_ready_starts_exactly_one_storage_task(tmp_path):
    watcher = _watcher(tmp_path)
    watcher._reconcile_targets = AsyncMock()
    await watcher.on_ready()
    first = watcher._storage_task
    assert first is not None
    await watcher.on_ready()
    assert watcher._storage_task is first
    assert not first.done()
    await watcher.close()
