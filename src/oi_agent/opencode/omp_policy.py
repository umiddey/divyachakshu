"""Second gate for omp tool calls: a pure, fail-closed decider.

``omp_policy.ts`` runs this module before every tool call. The shim passes the
tool_call event on stdin and the policy path in ``OI_OMP_POLICY``. The first
gate is the ``--tools`` allowlist on the omp argv. Any error here denies.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from oi_agent.opencode.policy import _DOC_EXTENSIONS, _SECRET_PATTERNS, normalize_write_dirs

_MODE_TOOLS: dict[str, frozenset[str]] = {
    "repo": frozenset({"read", "grep", "glob", "edit"}),
    "review": frozenset({"read", "grep", "glob"}),
    "external": frozenset({"read", "grep", "glob"}),
    "research": frozenset({"read", "web_search"}),
}
_WRITE_MODES = ("read-only", "docs-only")
# Trailing selectors that read strips before resolving: ``:50-100``, ``:raw``, ...
_SELECTOR_SUFFIX = re.compile(r":(?:raw|img|conflicts|[-0-9lL,+.:]+)$")
_GLOB_CHARS = ("*", "[")
_HOST_NAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
_NON_PUBLIC_SUFFIXES = (".local", ".internal", ".localhost", ".arpa")
# Entries a recursive grep scan may visit before the scope is denied.
_SCAN_LIMIT = 20_000
# Literal fragments of the secret patterns, for filters that may name them.
_SECRET_KEYWORDS = tuple(sorted({p.strip("*/").split("/")[-1].strip("*") for p in _SECRET_PATTERNS} - {""}))


@dataclass(frozen=True)
class Policy:
    """Validated per-call policy loaded from the 0600 policy file."""

    mode: str
    write_mode: str
    roots: tuple[Path, ...]
    cwd: Path
    write_dirs: tuple[str, ...] | None


def _absolute(entry: object) -> Path:
    if not isinstance(entry, str) or not os.path.isabs(entry):
        raise ValueError("policy paths must be absolute strings")
    return Path(entry).resolve()


def load_policy(path: Path) -> Policy:
    """Load and validate the policy file.

    Raises:
        ValueError: On an unknown mode or write mode, or malformed paths.
        KeyError: When a required key is missing.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    mode = raw["mode"]
    write_mode = raw["write_mode"]
    if mode not in _MODE_TOOLS:
        raise ValueError(f"unknown mode {mode!r}")
    if write_mode not in _WRITE_MODES:
        raise ValueError(f"unknown write mode {write_mode!r}")
    if not isinstance(raw["roots"], list):
        raise ValueError("roots must be a list")
    roots = tuple(_absolute(entry) for entry in raw["roots"])
    cwd = _absolute(raw["cwd"])
    if roots and not any(cwd.is_relative_to(root) for root in roots):
        raise ValueError("cwd must sit inside a root")
    write_dirs_raw = raw.get("write_dirs")
    if write_dirs_raw is not None and not isinstance(write_dirs_raw, list):
        raise ValueError("write_dirs must be a list or null")
    write_dirs = normalize_write_dirs(write_dirs_raw)
    return Policy(mode, write_mode, roots, cwd, tuple(write_dirs) if write_dirs else None)


def _peel(raw: str) -> str:
    """Strip trailing read selectors so ``file.ts:1-5`` checks as ``file.ts``."""
    previous = None
    while previous != raw:
        previous, raw = raw, _SELECTOR_SUFFIX.sub("", raw, count=1)
    return raw


def _is_secret(rel: str) -> bool:
    lowered = rel.lower()
    return any(fnmatchcase(lowered, pattern) for pattern in _SECRET_PATTERNS)


def _resolve_local(policy: Policy, raw: str) -> tuple[Path, Path, bool] | None:
    """Resolve a local path inside a root.

    Returns:
        ``(root, resolved, has_glob)`` or ``None`` to deny. A glob path is
        checked through its literal directory prefix; existence is checked
        only for literal paths.
    """
    if not raw or raw != raw.strip() or any(ord(ch) < 32 for ch in raw):
        return None
    path = _peel(raw)
    # "?" is a JSON selector, "~" and "file://" expand before resolution, and
    # any remaining ":" is an internal URL, archive, or SQLite selector.
    if "?" in path or path.startswith("~") or ":" in path or ".." in path.split("/"):
        return None
    has_glob = any(ch in path for ch in _GLOB_CHARS)
    base = path
    if has_glob:
        segments = path.split("/")
        cut = next(i for i, seg in enumerate(segments) if any(ch in seg for ch in _GLOB_CHARS))
        base = "/".join(segments[:cut]) or ("/" if path.startswith("/") else ".")
    candidate = Path(base)
    if not candidate.is_absolute():
        candidate = policy.cwd / candidate
    resolved = candidate.resolve()
    root = next((r for r in policy.roots if resolved.is_relative_to(r)), None)
    if root is None:
        return None
    if _is_secret(resolved.relative_to(root).as_posix()):
        return None
    if not has_glob and not resolved.exists():
        return None
    return root, resolved, has_glob


def _public_http_url(url: str) -> bool:
    """True only for http(s) URLs whose literal host is globally routable."""
    if not url or any(ord(ch) <= 32 or ch in ";\\" for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or "@" in parts.netloc or not host:
        return False
    if port is not None and not 0 < port < 65536:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return bool(_HOST_NAME.fullmatch(host)) and not host.endswith(_NON_PUBLIC_SUFFIXES)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _names_secret(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _SECRET_KEYWORDS)


def _secret_free_tree(root: Path, base: Path) -> bool:
    """True when no entry under ``base`` (recursive) is secret-named.

    grep returns file contents, so a recursive search over a tree that holds a
    secret-named entry would expose it. The scan is bounded: a tree above
    ``_SCAN_LIMIT`` entries is denied rather than partly checked.
    """
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(base):
        for name in (*dirnames, *filenames):
            scanned += 1
            if scanned > _SCAN_LIMIT or _is_secret(os.path.relpath(os.path.join(dirpath, name), root)):
                return False
    return True


def _read_like(policy: Policy, tool: str, tool_input: Mapping[str, Any]) -> tuple[bool, str]:
    default = "." if tool in ("grep", "glob") else None
    raw = tool_input.get("path", default)
    if not isinstance(raw, str):
        return False, f"{tool} needs a path"
    if tool == "glob":
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or pattern.startswith(("/", "~")) or ".." in pattern.split("/"):
            return False, "glob pattern is not allowed"
    filters = [tool_input.get("glob"), tool_input.get("include")]
    if tool == "glob":
        filters.append(tool_input.get("pattern"))
    if any(isinstance(item, str) and _names_secret(item) for item in filters):
        return False, f"{tool} filter targets secret-named files"
    for part in raw.split(";"):
        located = _resolve_local(policy, part)
        if located is None:
            return False, "path is outside the allowed roots or not allowed"
        root, resolved, has_glob = located
        if tool == "grep" and has_glob:
            return False, "grep needs a literal path"
        if tool == "grep" and resolved.is_dir() and not _secret_free_tree(root, resolved):
            return False, "grep scope holds secret-named files or is too large"
    return True, ""


def _edit_target_ok(policy: Policy, raw: str) -> bool:
    # edit targets carry no selectors; a colon is never a literal edit target.
    if ":" in raw or raw.startswith("~"):
        return False
    located = _resolve_local(policy, raw)
    if located is None:
        return False
    root, resolved, has_glob = located
    if has_glob or not resolved.is_file() or resolved.suffix.lower() not in _DOC_EXTENSIONS:
        return False
    if policy.write_dirs is None:
        return True
    rel = resolved.relative_to(root).as_posix()
    return any(rel.startswith(f"{scope}/") for scope in policy.write_dirs)


def _edit(policy: Policy, tool_input: Mapping[str, Any]) -> tuple[bool, str]:
    if policy.mode != "repo" or policy.write_mode != "docs-only":
        return False, "edit is not allowed in this mode"
    targets: list[Any] = []
    for key in ("path", "paths"):
        if key in tool_input:
            value = tool_input[key]
            targets.extend(value if isinstance(value, list) else [value])
    if not targets:
        return False, "edit needs a path"
    for raw in targets:
        if not isinstance(raw, str) or not _edit_target_ok(policy, raw):
            return False, "edit target is not an allowed document"
    return True, ""


def _research_read(tool_input: Mapping[str, Any]) -> tuple[bool, str]:
    raw = tool_input.get("path")
    if not isinstance(raw, str) or not _public_http_url(_peel(raw)):
        return False, "read is limited to public http(s) URLs in research mode"
    return True, ""


def decide(policy: Policy, tool: str, tool_input: Mapping[str, Any]) -> tuple[bool, str]:
    """Decide one tool call.

    Returns:
        ``(allowed, reason)``. Every path that is not an explicit allow denies.
    """
    if tool not in _MODE_TOOLS[policy.mode]:
        return False, f"{tool} is not allowed in {policy.mode} mode"
    if tool == "web_search":
        return True, ""
    if policy.mode == "research":
        return _research_read(tool_input)
    if tool == "edit":
        return _edit(policy, tool_input)
    return _read_like(policy, tool, tool_input)


def main() -> int:
    """Read one tool_call event from stdin and print a block verdict as JSON."""
    try:
        policy = load_policy(Path(os.environ["OI_OMP_POLICY"]))
        event = json.loads(sys.stdin.read())
        tool, tool_input = event["toolName"], event["input"]
        if not isinstance(tool, str) or not isinstance(tool_input, dict):
            raise ValueError("malformed tool_call event")
        allowed, reason = decide(policy, tool, tool_input)
    except Exception:  # any failure denies the call
        allowed, reason = False, "policy check failed"
    sys.stdout.write(json.dumps({"block": not allowed, "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
