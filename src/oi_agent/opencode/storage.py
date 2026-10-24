"""Bounded maintenance of OI's private OpenCode filesystem roots.

OI fills its own private npm cache (`home/.npm`) when the runtime binary is
resolved through an npx-style launcher. OI cleans up after itself: the cache
is pruned at daemon start and at most once per day, keeping only the npx
entry that holds the binary currently in use. Deletion never leaves the
private root and never raises into the daemon.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .paths import OpenCodePaths

logger = logging.getLogger(__name__)


def _is_within(resolved: Path, root: Path) -> bool:
    """Return True when `resolved` is `root` itself or inside it."""
    return resolved == root or root in resolved.parents


def _tree_bytes(path: Path) -> int:
    """Sum regular-file bytes under `path`, never following symlinks."""
    total = 0
    for current, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                candidate = Path(current) / name
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def prune_private_npm(paths: OpenCodePaths, keep: Path | None = None) -> int:
    """Delete OI's private npm caches under `home/.npm`; return bytes freed.

    Removes `home/.npm/_cacache` and every `home/.npm/_npx/<entry>` directory
    except the one containing `keep` (the resolved runtime binary, when it
    lives there). A target whose resolved path escapes `home/.npm` (symlink
    escape) is refused and left in place. Never raises: failures become one
    bounded category log line and pruning continues with the remaining
    targets.

    Args:
        paths: OI's private OpenCode filesystem roots.
        keep: Resolved binary path whose npx entry must survive.

    Returns:
        Bytes freed by this prune.
    """
    npm_root = paths.home / ".npm"
    freed = 0
    removed = 0
    try:
        if not npm_root.exists():
            logger.info("storage prune freed_bytes=%d removed=%d", freed, removed)
            return freed
        root = npm_root.resolve()
        targets: list[Path] = [npm_root / "_cacache"]
        npx_root = npm_root / "_npx"
        if npx_root.is_dir():
            targets.extend(sorted(npx_root.iterdir()))
        keep_dir: Path | None = None
        if keep is not None:
            try:
                keep_resolved = keep.resolve()
            except OSError:
                keep_resolved = None
            if keep_resolved is not None:
                for entry in targets[1:]:
                    try:
                        if keep_resolved.is_relative_to(entry.resolve()):
                            keep_dir = entry.resolve()
                            break
                    except OSError:
                        continue
        for target in targets:
            try:
                if not target.exists() and not target.is_symlink():
                    continue
                resolved = target.resolve()
                if not _is_within(resolved, root):
                    logger.warning("storage prune refused category=symlink_escape")
                    continue
                if keep_dir is not None and resolved == keep_dir:
                    continue
                size = _tree_bytes(resolved)
                shutil.rmtree(resolved)
                freed += size
                removed += 1
            except OSError as exc:
                logger.warning(
                    "storage prune error category=%s", type(exc).__name__
                )
    except Exception as exc:  # noqa: BLE001 - cleanup must never reach the daemon
        logger.warning("storage prune error category=%s", type(exc).__name__)
    logger.info("storage prune freed_bytes=%d removed=%d", freed, removed)
    return freed


def root_usage(paths: OpenCodePaths) -> list[tuple[str, Path, int]]:
    """Measure the sizes of OI's private roots, read-only.

    Args:
        paths: OI's private OpenCode filesystem roots.

    Returns:
        One (label, path, bytes) row per private root.
    """
    rows: list[tuple[str, Path, int]] = []
    for label, path in (
        ("home/.npm", paths.home / ".npm"),
        ("data", paths.data_home),
        ("state", paths.state_home),
        ("cache", paths.cache_home),
    ):
        try:
            size = _tree_bytes(path) if path.exists() else 0
        except OSError as exc:
            logger.warning("storage usage error category=%s", type(exc).__name__)
            size = 0
        rows.append((label, path, size))
    return rows


def human_bytes(size: int) -> str:
    """Render a byte count in bounded human units (B/KiB/MiB/GiB)."""
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"
