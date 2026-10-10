"""Fail-closed OpenCode v2 permission policy for the native OI agent.

Native v2 contract (https://opencode.ai/v2/docs/permissions/):

* Rules live in ``permissions`` (NOT v1 ``permission``) as an ordered list of
  ``{"action", "resource", "effect"}`` objects. The last matching rule wins, so
  broad rules come first and exceptions follow.
* Action names are v2: ``shell`` and ``subagent`` replace v1 ``bash`` and
  ``task``; ``execute`` is additional execution authority and stays denied.
  ``lsp`` and ``doom_loop`` are NOT v2 permission actions and are not used
  here.
* ``agents.<id>.system`` carries the system prompt; ``agents.<id>.permissions``
  carries the agent's own ordered rules, appended after global and default
  rules.
* Native v2 ships a broad allow-by-default base policy
  (``{"action": "*", "resource": "*", "effect": "allow"}`` plus a few ``ask``
  rules). Confinement therefore only holds if this policy DENIES first, so
  every policy returned here starts with a broad deny and only then re-grants
  the narrowest possible repository-scoped reads.
* ``*`` matches zero or more characters including ``/``; ``?`` matches exactly
  one. Resources are whole values, so patterns below express "at any depth"
  without v1's ``**/`` prefix idiom.

``write_mode="docs-only"`` optionally grants ``edit`` (which covers the edit,
write, and patch tools) for ``*.md``/``*.txt`` only. Secrets stay denied,
code stays denied, and every other action stays denied.
"""

from __future__ import annotations

from pathlib import Path

# Read-only discovery actions that stay available, and the shell/subagent
# actions that never do.
_READ_ACTIONS = ("read", "glob")
_DENIED_ACTIONS = (
    "shell", "subagent", "skill", "question", "webfetch", "websearch",
    "execute", "edit",
)
_WRITE_MODES = ("read-only", "docs-only")
_DOC_EXTENSIONS = (".md", ".txt")

# Whole-value patterns. ``*`` spans ``/``, so each pattern covers every depth
# without a separate ``**/`` form.
_SECRET_PATTERNS = (
    # Nested git directories are denied too: only denying a root-level ".git"
    # would leave submodules and nested worktrees readable.
    ".git", ".git/*", "*.git", "*.git/*", "*/.git", "*/.git/*",
    ".env", ".env.*", "*.env", "*.env.*",
    "*.pem", "*.key", "*.keytab", "*.p12", "*.pfx", "*.crt", "*.cer",
    "*id_rsa", "*id_ed25519",
    "*credentials*", "*secret*", "*token*",
    "*.netrc", "*secrets*", "*.htpasswd", "*password*",
)


def _rule(action: str, resource: str, effect: str) -> dict[str, str]:
    """Build one native v2 permission rule."""
    return {"action": action, "resource": resource, "effect": effect}


def normalize_write_dirs(write_dirs: list[str] | tuple[str, ...] | None) -> list[str] | None:
    """Normalize optional repo-relative doc-write scopes.

    Args:
        write_dirs: Raw directory entries from config.

    Returns:
        ``None`` for an empty/unset scope, otherwise the cleaned relative
        paths (no trailing slash, no ``.``/``..``/absolute). ``None`` means
        "any doc in the repository", which is the documented default.

    Raises:
        ValueError: When scopes were supplied but every entry was invalid.
            Collapsing a malformed scope to "no scope" would silently WIDEN
            the grant to every ``.md``/``.txt`` in the repository, which is
            the opposite of failing closed. Config validation normally rejects
            such input earlier; this keeps a direct policy caller safe too.
    """
    if not write_dirs:
        return None
    cleaned: list[str] = []
    for entry in write_dirs:
        if not isinstance(entry, str):
            continue
        if entry.strip().startswith("/"):
            continue
        norm = entry.strip().strip("/")
        if not norm or norm == ".":
            continue
        if ".." in norm.split("/"):
            continue
        if any(not part for part in norm.split("/")):
            continue
        if norm not in cleaned:
            cleaned.append(norm)
    if not cleaned:
        raise ValueError(
            "write_dirs were supplied but none were usable; refusing to widen "
            "doc writes to the whole repository"
        )
    return cleaned


def _secret_rules(action: str) -> list[dict[str, str]]:
    """Deny every secret pattern for one action."""
    return [_rule(action, pattern, "deny") for pattern in _SECRET_PATTERNS]


def _doc_edit_rules(write_dirs: list[str] | None) -> list[dict[str, str]]:
    """Build fail-closed ``edit`` rules for ``*.md``/``*.txt`` docs only.

    A configured scope grants only INSIDE that directory. Because ``*`` spans
    ``/``, the boundary pattern ``<scope>/*<ext>`` already covers nested files
    such as ``docs/deep/note.md``, while a ``<scope>*<ext>`` pattern would also
    match a sibling directory sharing the prefix (``docs-private/plan.md``).
    Only the boundary form is emitted.
    """
    rules = [_rule("edit", "*", "deny")]
    if write_dirs:
        for scope in write_dirs:
            for ext in _DOC_EXTENSIONS:
                rules.append(_rule("edit", f"{scope}/*{ext}", "allow"))
    else:
        for ext in _DOC_EXTENSIONS:
            rules.append(_rule("edit", f"*{ext}", "allow"))
    rules.extend(_secret_rules("edit"))
    return rules


def build_permission_policy(
    repo_root: Path,
    additional_repo_roots: list[Path] | None = None,
    *,
    write_mode: str = "read-only",
    write_dirs: list[str] | tuple[str, ...] | None = None,
    controller_snapshots: bool = False,
) -> list[dict[str, str]]:
    """Return the ordered fail-closed OpenCode v2 permission rules.

    OpenCode matches ``read``/``edit``/``external_directory`` resources against
    the active Location, which OI sets to the primary repository via the
    subprocess working directory. When additional repositories are authorized,
    each is granted an explicit ``external_directory`` boundary and its own
    ``read`` scope; every other path outside the authorized roots stays denied.

    ``glob`` resources are the requested pattern, so secret patterns are denied
    there too and no glob can widen access outside the repository.

    Args:
        repo_root: Canonical primary repository root used as OpenCode cwd.
        additional_repo_roots: Optional auxiliary repository roots.
        write_mode: ``"read-only"`` (default) or ``"docs-only"`` (allow
            ``edit`` for ``*.md``/``*.txt`` only). Unknown modes fail closed.
        write_dirs: Optional repo-relative scopes for doc writes; empty means
            any ``.md``/``.txt`` in the repository.
        controller_snapshots: True only when ``repo_root`` and every
            additional root is a controller-materialized review snapshot:
            a verified-SHA tree the trusted controller wrote itself, with
            symlinks rejected and no ``.git`` inside. Such roots are never
            worktree roots, so the ``.git`` boundary check below would
            fail-closed deny every read of the very trees the review must
            audit. With the attestation, grants are ABSOLUTE path prefixes
            instead of Location-relative allows, so a relative path that
            escapes the snapshot directory simply matches no allow and stays
            denied. Ordinary repository audits never set this.

    Returns:
        Ordered native v2 permission rules.
    """
    primary = repo_root.expanduser().resolve()
    # Broad deny FIRST: it overrides the native allow-by-default base policy
    # and every later rule re-grants only what OI deliberately allows.
    rules: list[dict[str, str]] = [_rule("*", "*", "deny")]

    # Location confinement. Native v2 treats the active Location (the working
    # directory) as the project root for relative paths, but a relative path
    # may still leave the Location while staying inside the enclosing project
    # worktree, and such a path needs no external_directory approval. So when
    # the authorized root is NOT a worktree root of its own, a sibling file in
    # the parent worktree would be readable under a plain relative allow. That
    # confinement cannot be expressed as a prefix rule, so OI fails closed and
    # grants no repository reads at all rather than over-claiming isolation.
    location_is_worktree_root = (primary / ".git").exists()
    if location_is_worktree_root or controller_snapshots:
        # Native v2 confines relative read/glob resources to the active
        # Location and only allows paths OUTSIDE it through explicit
        # absolute-prefix rules, so a relative allow cannot escape the
        # snapshot tree (proven on real native: relative in-Location reads
        # succeed, "../escape" and absolute-outside reads are denied, and
        # only the granted auxiliary prefix is readable).
        for action in _READ_ACTIONS:
            rules.append(_rule(action, "*", "allow"))
            rules.extend(_secret_rules(action))
    else:
        for action in _READ_ACTIONS:
            rules.append(_rule(action, "*", "deny"))

    for action in _DENIED_ACTIONS:
        rules.append(_rule(action, "*", "deny"))

    if write_mode == "docs-only":
        scopes = normalize_write_dirs(write_dirs)
        if location_is_worktree_root:
            rules.extend(_doc_edit_rules(scopes))
        else:
            # Without a provable Location boundary no edit is safe: relative
            # paths could leave the configured scope (and even the intended
            # repository) while staying inside an enclosing worktree. This
            # holds for a configured scope too, not only "any doc".
            # Controller snapshots are always read-only runs.
            rules.append(_rule("edit", "*", "deny"))
    else:
        rules.append(_rule("edit", "*", "deny"))

    # Explicit external-directory boundaries: the authorized auxiliary roots
    # only. ``external_directory`` defaults to "ask" natively, so the broad
    # deny above must be paired with explicit denies and narrow allows.
    rules.append(_rule("external_directory", "*", "deny"))
    for extra in additional_repo_roots or ():
        boundary = extra.expanduser().resolve()
        # An auxiliary root is only granted when it is its own worktree root:
        # otherwise reads could escape into the parent worktree exactly as the
        # Location case above. Controller-materialized snapshots carry the
        # caller's attestation instead of a ``.git`` boundary.
        if not (boundary / ".git").exists() and not controller_snapshots:
            continue
        absolute = str(boundary)
        rules.append(_rule("external_directory", f"{absolute}/*", "allow"))
        rules.append(_rule("read", f"{absolute}/*", "allow"))
        for pattern in _SECRET_PATTERNS:
            rules.append(_rule("read", f"{absolute}/{pattern}", "deny"))
            rules.append(_rule("external_directory", f"{absolute}/{pattern}", "deny"))
    return rules


def build_agent_config(
    model: str,
    steps: int,
    repo_root: Path,
    prompt: str,
    additional_repo_roots: list[Path] | None = None,
    *,
    write_mode: str = "read-only",
    write_dirs: list[str] | tuple[str, ...] | None = None,
    controller_snapshots: bool = False,
) -> dict[str, object]:
    """Build the native primary ``agents.oi`` OpenCode v2 configuration.

    Args:
        model: Model provider identifier.
        steps: Step limit.
        repo_root: Canonical primary repository root.
        prompt: Embedded system prompt, carried by the v2 ``system`` field.
        additional_repo_roots: Optional auxiliary repository roots.
        write_mode: ``"read-only"`` or ``"docs-only"`` (unknown fails closed).
        write_dirs: Optional repo-relative scopes for doc writes.
        controller_snapshots: Attestation that every authorized root is a
            controller-materialized, verified-SHA review snapshot (see
            :func:`build_permission_policy`).

    Returns:
        JSON-compatible native v2 agent configuration.
    """
    docs = write_mode == "docs-only"
    agent_rules = build_permission_policy(
        repo_root,
        additional_repo_roots=additional_repo_roots,
        write_mode=write_mode,
        write_dirs=write_dirs,
        controller_snapshots=controller_snapshots,
    )
    return _envelope({
        "agents": {
            "oi": {
                "description": (
                    "Discord repository auditor with docs-only write"
                    if docs else "Read-only Discord repository auditor"
                ),
                "mode": "primary",
                "model": model,
                "system": prompt,
                "steps": steps,
                "permissions": agent_rules,
            }
        }
    })


def _envelope(document: dict) -> dict:
    """Wrap an agent document in the run-wide fail-closed envelope.

    Two invariants live here rather than in each caller:

    * A top-level ``permissions`` deny-all covers every agent that does NOT
      carry its own ordered rules. Native v2 hidden maintenance agents
      (notably ``compaction``) keep the broad allow-by-default base policy, so
      without this they would run with full authority inside a read-only
      audit. Per-agent rules are appended after global rules and stay narrow.
    * Automatic workspace mutation is switched off. Native snapshot and
      formatter side paths would otherwise write into a repository during a
      read-only run.
    """
    return {
        "permissions": [_rule("*", "*", "deny")],
        "snapshot": False,
        "formatter": False,
        **document,
    }


def build_tool_free_agent(
    model: str,
    prompt: str,
    *,
    steps: int = 1,
) -> dict[str, object]:
    """Build a native v2 agent with no tools at all.

    Used for bounded one-shot interpretation queries (for example extracting a
    history-window spec). The broad deny is not merely a read policy: with
    every action denied the agent has no repository, network, or filesystem
    authority, matching the v1 ``{"*": "deny"}`` intent under the v2 contract.

    The run-wide envelope applies here too: the top-level deny-all keeps
    native v2's hidden maintenance agents (notably ``compaction``) from
    running under the allow-by-default base policy during interpretation
    queries, and automatic snapshot/formatter mutation stays off.
    """
    return _envelope({
        "agents": {
            "oi_eval": {
                "description": "Bounded interpretation agent with no tools",
                "mode": "primary",
                "model": model,
                "system": prompt,
                "steps": steps,
                "permissions": [_rule("*", "*", "deny")],
            }
        }
    })
