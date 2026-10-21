"""Read-scoped repository refresh and snapshot provenance."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CAUSE_UNTRACKED = "untracked_would_be_overwritten"
CAUSE_LOCAL_CHANGES = "local_changes"
CAUSE_DIVERGED = "diverged"
CAUSE_NO_UPSTREAM = "no_upstream"
CAUSE_AUTH_OR_NETWORK = "auth_or_network"
CAUSE_OTHER = "other"

_NO_UPSTREAM_MARKERS = ("no tracking information",)
_DIVERGED_MARKERS = (
    "not possible to fast-forward",
    "diverging branches",
    "divergent branches",
)
_AUTH_NETWORK_MARKERS = (
    "could not read from remote repository",
    "does not appear to be a git repository",
    "unable to access",
    "could not resolve host",
    "authentication failed",
    "connection refused",
    "connection timed out",
)

# Last successful pull per repo (normalized path), for this process only.
_LAST_SUCCESSFUL_PULL: dict[str, str] = {}


@dataclass(frozen=True)
class PullResult:
    """Outcome of one fast-forward pull attempt on a watched clone."""

    ok: bool
    cause: str = ""
    blocking_paths: tuple[str, ...] = ()
    behind: int = 0
    upstream: str = ""
    last_pull: str | None = None


def _git(repo_root: Path, *args: str) -> str | None:
    """Run a bounded read-only Git command without logging its output."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _pull_upstream(repo_path: str) -> str:
    name = _git(
        Path(repo_path),
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    return name or ""


def _behind_count(repo_path: str, upstream: str) -> int:
    if not upstream:
        return 0
    out = _git(Path(repo_path), "rev-list", "--count", f"HEAD..{upstream}")
    try:
        return int(out or "")
    except ValueError:
        return 0


def _run_pull(repo_path: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", repo_path, "-c", "pull.rebase=false", "pull",
             "--ff-only", "--autostash"],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _blocking_paths_after(lines: list[str], start: int) -> tuple[list[str], int]:
    paths: list[str] = []
    i = start + 1
    while i < len(lines) and lines[i].startswith("\t"):
        raw = lines[i].strip()
        if raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1]
        if raw:
            paths.append(raw)
        i += 1
    return paths, i


def _classify_pull_failure(output: str) -> tuple[str, tuple[str, ...]]:
    """Map raw git output to a stable cause class; message text is never kept."""
    lowered = output.lower()
    if any(marker in lowered for marker in _NO_UPSTREAM_MARKERS):
        return CAUSE_NO_UPSTREAM, ()
    lines = output.splitlines()
    cause = ""
    paths: list[str] = []
    i = 0
    while i < len(lines):
        low = lines[i].lower()
        if "untracked working tree files would be overwritten" in low:
            cause = CAUSE_UNTRACKED
            block, i = _blocking_paths_after(lines, i)
            paths.extend(block)
            continue
        if "local changes to the following files would be overwritten" in low:
            cause = CAUSE_LOCAL_CHANGES
            block, i = _blocking_paths_after(lines, i)
            paths.extend(block)
            continue
        i += 1
    if cause:
        return cause, tuple(paths)
    if any(marker in lowered for marker in _DIVERGED_MARKERS):
        return CAUSE_DIVERGED, ()
    if any(marker in lowered for marker in _AUTH_NETWORK_MARKERS):
        return CAUSE_AUTH_OR_NETWORK, ()
    return CAUSE_OTHER, ()


def git_pull(repo_path: str) -> PullResult:
    """Refresh a watched clone with a read-scoped fast-forward pull.

    Returns a structured outcome: success flag, a stable cause class for
    failures, the paths git named as blocking the merge, how far behind the
    upstream the clone is after the fetch, and the timestamp of the last
    successful pull for this repo in this process.
    """
    memory_key = str(Path(repo_path).resolve())
    upstream = _pull_upstream(repo_path)
    last_pull = _LAST_SUCCESSFUL_PULL.get(memory_key)
    outcome = _run_pull(repo_path)
    if outcome is not None and outcome.returncode == 0:
        now = datetime.now(timezone.utc).isoformat()
        _LAST_SUCCESSFUL_PULL[memory_key] = now
        return PullResult(ok=True, upstream=upstream, last_pull=now)
    if outcome is None:
        logger.warning("[provenance] git pull unavailable")
        return PullResult(
            ok=False,
            cause=CAUSE_OTHER,
            behind=_behind_count(repo_path, upstream),
            upstream=upstream,
            last_pull=last_pull,
        )
    cause, blocking = _classify_pull_failure(f"{outcome.stderr}\n{outcome.stdout}")
    return PullResult(
        ok=False,
        cause=cause,
        blocking_paths=blocking,
        behind=_behind_count(repo_path, upstream),
        upstream=upstream,
        last_pull=last_pull,
    )


def staleness_note(name: str, result: PullResult) -> str:
    """One-line staleness warning for the model; cause class and counts only."""
    if result.ok:
        return ""
    updated = result.last_pull or "not since OI started"
    branch = result.upstream or "its upstream"
    return (
        f"reading an old copy of {name}: {result.behind} commits behind {branch}, "
        f"last updated {updated}; cause: {result.cause or CAUSE_OTHER}"
    )


def audit_sha(repo_root: Path) -> str:
    """Return short HEAD SHA with an honest dirty suffix."""
    sha = _git(repo_root, "rev-parse", "--short", "HEAD") or "unknown"
    return f"{sha}-dirty" if _git(repo_root, "status", "--porcelain") else sha


def worktree_fingerprint(repo_root: Path) -> str:
    """Hash HEAD, status, and diff to detect any mid-run repository mutation."""
    head = _git(repo_root, "rev-parse", "HEAD") or "unknown"
    status = _git(repo_root, "status", "--porcelain") or ""
    diff = _git(repo_root, "diff", "HEAD") or ""
    if head == "unknown":
        return "unknown"
    payload = f"{head}\n{status}\n{diff}".encode("utf-8", errors="replace")
    return hashlib.sha1(payload).hexdigest()[:12]


MIDAUDIT_MUTATION_WARNING = "repository changed during the audit"


def mutation_warning(repo_root: Path, fingerprint_before: str) -> str:
    """Return the stable provenance warning when the worktree changed."""
    return (MIDAUDIT_MUTATION_WARNING
            if worktree_fingerprint(repo_root) != fingerprint_before else "")
