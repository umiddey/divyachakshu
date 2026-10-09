"""Ownership markers and startup sweep for OI's private temp namespaces.

Disposable native roots (``oi-native-*``) and prepared-input workdirs
(``oi-external-*``) are deleted deterministically when a run ends. If the
process dies mid-run they would leak into the shared temp directory, and a
naive startup sweep could delete ANOTHER live OI instance's active roots.

The rule is therefore: every OI temp root carries an owner marker naming the
creating process and boot; a sweep may remove a root ONLY when the marker
exists, was written in the CURRENT boot, and its pid is provably dead.
Anything else — a markerless directory, another boot's leftovers, a live
pid — is never swept.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

OI_TEMP_PREFIXES = ("oi-native-", "oi-external-")
_OWNER_MARKER = "owner.json"


def boot_id() -> str:
    """Return the current boot identifier, or empty when unavailable."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, ValueError):
        return ""


def mark_owner(root: Path) -> None:
    """Write this process's ownership marker into one temp root. Best effort."""
    try:
        (root / _OWNER_MARKER).write_text(json.dumps({
            "pid": os.getpid(),
            "boot": boot_id(),
            "created": time.time(),
        }), encoding="ascii")
    except (OSError, ValueError):
        pass


def _pid_alive(pid: int) -> bool:
    """Return True when a process with this pid exists right now."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def sweep_abandoned_temp_roots(
    prefixes: tuple[str, ...] = OI_TEMP_PREFIXES,
    *,
    tmp_dir: Path | None = None,
    current_boot: str | None = None,
    alive=None,
) -> int:
    """Remove only provably abandoned OI temp roots; never touch anything else.

    A directory is removed only when it carries an owner marker written in
    the current boot whose pid no longer exists. Markerless directories,
    foreign files, and live-owner roots are left untouched, so this can never
    sweep another active OI instance's or another program's temp prefixes.

    Args:
        prefixes: The owned namespace prefixes.
        tmp_dir: The temp directory to scan (default: the process TMPDIR).
        current_boot: Override for tests; defaults to :func:`boot_id`.
        alive: Override liveness predicate for tests; defaults to pid check.

    Returns:
        The number of directories removed.
    """
    check_boot = boot_id() if current_boot is None else current_boot
    is_alive = _pid_alive if alive is None else alive
    scan_root = Path(tmp_dir if tmp_dir is not None else
                     os.environ.get("TMPDIR", "/tmp"))
    removed = 0
    try:
        candidates = list(scan_root.iterdir())
    except OSError:
        return 0
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        if not any(candidate.name.startswith(prefix) for prefix in prefixes):
            continue
        try:
            marker = json.loads(
                (candidate / _OWNER_MARKER).read_text(encoding="ascii")
            )
        except (OSError, ValueError):
            continue
        if not isinstance(marker, dict):
            continue
        if marker.get("boot") != check_boot:
            continue
        pid = marker.get("pid")
        if not isinstance(pid, int) or is_alive(pid):
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        if not candidate.exists():
            removed += 1
    return removed
