"""Async OpenCode subprocess runner with bounded output and process cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from contextlib import AsyncExitStack
from dataclasses import replace as _dc_replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..store import Store

from ..agent.reply import Reply
from ..config import Config, WatchTarget
from ..external.prepare import PreparedExternal
from ..external.records import (
    STATUS_IMAGE,
    STATUS_UNSUPPORTED,
    AttachmentInput,
)
from ..external.research import (
    CLASSIFIER_SYSTEM,
    ResearchResult,
    failure_text,
    new_empty_research_dir,
    parse_research_output,
)
from ..watch.context_request import (
    ContextRequest,
    mentions_expansion,
    parse_explicit_request,
    parse_spec,
)
from .bootstrap import resolve_runtime_binary
from .native import (
    DisposableNativeRoot,
    NativeCompletion,
    build_argv,
    config_content,
    copy_isolated_credentials,
    copy_model_catalog,
    initialize_native_schema,
    native_db_path,
    verify_completion,
)
from .paths import resolve_opencode_paths
from .policy import (
    build_agent_config,
    build_research_agent,
    build_tool_free_agent,
    normalize_write_dirs,
)
from .prompt import FINAL_OUTPUT_MARKER, build_prompt, render_evidence_block
from .provenance import audit_sha, git_pull, mutation_warning, worktree_fingerprint
from .review_snapshots import ReviewSnapshot, ReviewSnapshotError, materialized_review
from .review_target import (
    PROVIDER_GITHUB,
    PROVIDER_GITLAB,
    ReviewRefError,
    ReviewTarget,
    resolve_review_target,
)

logger = logging.getLogger(__name__)

MAX_JSON_LINE_BYTES = 128 * 1024
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 32 * 1024
# Bounded stdin (plan Phase 8 item 6): prompts above this size fail closed
# before any subprocess is spawned.
MAX_PROMPT_BYTES = 256 * 1024

# One-shot spec extraction for chat-driven context expansion (plan
# 20260920_chat_context_expansion). The model only interprets; the harness
# fetches, clamped by config caps. Output contract: one JSON object, nothing
# else — parsed by watch.context_request.parse_spec which fails closed.
_CONTEXT_SPEC_PROMPT_TEMPLATE = (
    "You read a Discord message and extract only a conversation-history "
    "window request. Today is {today}. Output ONLY one minified JSON object, "
    "no prose, no markdown fences, exactly this shape:\n"
    '{{"count": <integer|null>, "since_days": <integer|null>, '
    '"since_date": <"YYYY-MM-DD"|null>}}\n'
    'Rules: "count" = how many past messages to include (null if not asked); '
    '"since_days" = whole-day look-back window for relative asks like "2 weeks '
    'ago" (null if not asked); "since_date" = a NAMED calendar date converted '
    'to YYYY-MM-DD — use the START of that day, e.g. "from 10th Sept 2026" -> '
    '"2026-09-10", "after March 3" -> the year implied by context or today\'s '
    'year. If asked to "read earlier", "go back further", or read the whole '
    'thread without a number, set "count" to 200. '
    'Examples: "read the last 100 messages" -> '
    '{{"count":100,"since_days":null,"since_date":null}}; '
    '"messages since 2 weeks ago" -> '
    '{{"count":null,"since_days":14,"since_date":null}}; '
    '"messages frm 10th Sept 2026 onwards" -> '
    '{{"count":null,"since_days":null,"since_date":"2026-09-10"}}; '
    '"read earlier" -> {{"count":200,"since_days":null,"since_date":null}}; '
    '"summarize the thread" -> '
    '{{"count":null,"since_days":null,"since_date":null}}.\n\n'
    "Discord message:\n"
)


def _context_spec_prompt() -> str:
    """Return the spec prompt anchored to today's UTC date."""
    return _CONTEXT_SPEC_PROMPT_TEMPLATE.format(
        today=datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )

_repo_locks: dict[str, asyncio.Lock] = {}
_repo_locks_guard = asyncio.Lock()


def scope_id_for_target(target: WatchTarget) -> str:
    """Return the ONE canonical memory scope key for a conversation target.

    Guild-wide watches scope memory to the guild id; channel watches scope it
    to the channel id. The prompt fetch in ``_run_locked`` and the reflection
    enqueue in the Discord client MUST both use this rule (finding B1) so a
    stored profile is always readable by the next prompt for the same scope.
    """
    return str(target.guild_id if target.is_guild_watch else target.channel_id)


class _ProtocolError(RuntimeError):
    """Internal bounded JSONL protocol failure."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


async def _repo_lock(repo_path: str) -> asyncio.Lock:
    """Return the process-wide async lock for one canonical repository."""
    key = str(Path(repo_path).expanduser().resolve())
    async with _repo_locks_guard:
        lock = _repo_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _repo_locks[key] = lock
        return lock


async def _acquire_repo_locks(repo_paths: list[str]) -> AsyncExitStack:
    """Acquire async locks for all involved repositories in canonical sorted order.

    Sorting paths lexicographically guarantees deadlock-free lock acquisition
    across concurrent multi-repository audits.

    Args:
        repo_paths: List of repository paths to lock.

    Returns:
        AsyncExitStack: Managed context holding all acquired repository locks.
    """
    stack = AsyncExitStack()
    canonical_paths = sorted({str(Path(p).expanduser().resolve()) for p in repo_paths if p})
    for path_str in canonical_paths:
        lock = await _repo_lock(path_str)
        await stack.enter_async_context(lock)
    return stack


async def _consume_jsonl(reader: asyncio.StreamReader) -> tuple[list[dict[str, Any]], int]:
    """Consume bounded JSONL without retaining raw output or evidence."""
    events: list[dict[str, Any]] = []
    total = 0
    pending = b""
    while True:
        chunk = await reader.read(8192)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_STDOUT_BYTES:
            raise _ProtocolError("stdout_limit")
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            if len(line) > MAX_JSON_LINE_BYTES:
                raise _ProtocolError("json_line_limit")
            try:
                event = json.loads(line.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 - protocol boundary
                raise _ProtocolError("malformed_jsonl") from exc
            if not isinstance(event, dict):
                raise _ProtocolError("malformed_jsonl")
            events.append(event)
    if pending.strip():
        if len(pending) > MAX_JSON_LINE_BYTES:
            raise _ProtocolError("json_line_limit")
        try:
            event = json.loads(pending.decode("utf-8"))
            if isinstance(event, dict):
                events.append(event)
        except Exception as exc:  # noqa: BLE001
            raise _ProtocolError("malformed_jsonl") from exc
    return events, total


async def _consume_stderr(reader: asyncio.StreamReader) -> tuple[int, str]:
    """Drain stderr to prevent pipe blockage, returning (byte_count, sample_text)."""
    sample = b""
    total = 0
    while True:
        chunk = await reader.read(4096)
        if not chunk:
            break
        total += len(chunk)
        if len(sample) < MAX_STDERR_BYTES:
            sample += chunk[:MAX_STDERR_BYTES - len(sample)]
    return total, sample.decode("utf-8", errors="replace").strip()


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Terminate the OpenCode process group, escalating to SIGKILL."""
    if process.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    except OSError as exc:
        logger.warning("[opencode] SIGTERM delivery failed: %s", exc)
    try:
        await asyncio.wait_for(process.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


# Dict-derived error classes are embedded verbatim in Discord-bound failure
# text (finding P1-2), so each candidate must be a bounded enum-like
# identifier: no whitespace, newlines, control characters, or path/URL syntax.
_ERROR_CLASS_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _error_class(event: dict[str, Any]) -> str:
    """Classify an error event without returning its potentially sensitive body.

    Dict payloads contribute only their short enum-like ``name``/``type``:
    a candidate is used only when it is a string fully matching
    ``[A-Za-z0-9_.-]{1,64}``; otherwise the next candidate is tried and, when
    none validates, the payload collapses to ``opencode_error``.
    Plain-string payloads are unbounded harness diagnostics: the only signal
    kept is the ``session`` substring (``unknown_session``); everything else
    collapses to the bounded constant ``opencode_error`` so no raw harness
    text can reach a reply.
    """
    err = event.get("error") or event.get("data")
    if isinstance(err, dict):
        for candidate in (err.get("name"), err.get("type")):
            if isinstance(candidate, str) and _ERROR_CLASS_PATTERN.fullmatch(candidate):
                return candidate
        return "opencode_error"
    if isinstance(err, str):
        if "session" in err.lower():
            return "unknown_session"
        return "opencode_error"
    return "opencode_error"


# Pinned OpenCode ``run --format json`` event contract (verified against a
# controlled local v1.18.x run and the public part schema):
#
#   {"type": "step_start",  "timestamp": int, "sessionID": "ses_...",
#    "part": {"id": "prt_...", "messageID": "msg_...", "type": "step-start"}}
#   {"type": "text",        ...
#    "part": {"id": "prt_...", "messageID": "msg_...", "type": "text",
#             "text": str, "time": {"start": int, "end": int | missing},
#             "synthetic"?: bool, "ignored"?: bool}}
#   {"type": "tool_use",    ...
#    "part": {"id": "prt_...", "messageID": "msg_...", "type": "tool",
#             "tool": str, "callID": str,
#             "state": {"status": "pending" | "running" | "completed" | "error",
#                       "input": {...}, "output": str}}}
#   {"type": "step_finish", ...
#    "part": {"id": "prt_...", "messageID": "msg_...", "type": "step-finish",
#             "reason": str, "tokens": {...}, "cost": number}}
#   {"type": "error", "timestamp": int, "sessionID": "ses_...",
#    "error": {"name": str, "data": {...}}}          (no ``part``)
#
# ``reason`` is a free-form string; observed values are ``tool-calls`` for
# steps that ended to run tools (their text is narration) and ``stop`` for the
# successful terminal answer. Non-successful terminal endings (``length``,
# ``content-filter``, ``error``, ...) are possible per the public schema. A
# resumed ``--session`` stream contains only the new turn, so each run yields
# at most one ``stop``-completed assistant message.
_KNOWN_EVENT_TYPES = frozenset({
    "step_start", "step_finish", "text", "tool_use", "reasoning", "error",
})
_ANSWER_COMPLETED_REASON = "stop"
_TOOL_CALLS_REASON = "tool-calls"



def _stream_identity(
    events: list[dict[str, Any]],
) -> tuple[str | None, str | None, bool]:
    """Return ``(session_id, last_text_message_id, had_error_event)``.

    These are the exact identifiers completion proof is bound to. Only
    metadata is read: no text, no tool input, no tool output.
    """
    session_id: str | None = None
    message_id: str | None = None
    had_error = False
    for event in events:
        session_id = event.get("sessionID") or session_id
        event_type = event.get("type")
        if event_type == "error":
            had_error = True
            continue
        part = event.get("part")
        if isinstance(part, dict) and event_type == "text":
            candidate = part.get("messageID")
            if isinstance(candidate, str) and candidate:
                message_id = candidate
    return session_id, message_id, had_error





def _select_final_text_v2(
    events: list[dict[str, Any]],
    completion: NativeCompletion,
) -> tuple[str, str | None, str | None, int]:
    """Select the one completed native v2 answer, bound to native proof.

    Native v2 ``run --format json`` does NOT emit a terminal ``step_finish``
    for the final assistant answer, so the v1 selector can never see a
    completed answer on a working v2 install. Accepting a bare text event
    instead would make truncated or narration text deliverable, so completion
    is required as an input: the caller proves from native state that the
    exact message the CLI last reported really finished with ``stop``.

    The stream envelope is validated exactly as strictly as before (known
    event types, well-formed parts, consistent part ids, no text after a
    closed message), so a malformed or injected stream still fails closed.
    Narration messages from tool steps are structurally excluded: only the
    message the CLI reported last, and only if it is the final message in the
    stream, can contribute text.

    Args:
        events: Parsed JSONL events in stream order.
        completion: Authoritative native completion proof for the run.

    Returns:
        Tuple of final text, session id, bounded error class, and visible
        part count.
    """
    session_id: str | None = None
    text_parts = 0
    messages: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    last_text_message: str | None = None
    stream_error: str | None = None

    for event in events:
        session_id = event.get("sessionID") or session_id
        event_type = event.get("type")
        if event_type not in _KNOWN_EVENT_TYPES:
            return "", session_id, "protocol_unknown_event", 0
        if event_type == "error":
            stream_error = stream_error or _error_class(event)
            continue
        part = event.get("part")
        if not isinstance(part, dict):
            return "", session_id, "protocol_malformed_part", 0
        message_id = part.get("messageID")
        if not isinstance(message_id, str) or not message_id:
            return "", session_id, "protocol_missing_message_id", 0
        message = messages.get(message_id)
        if message is None:
            message = {"by_id": {}, "order": [], "closed": False}
            messages[message_id] = message
            order.append(message_id)
        if event_type == "step_finish":
            reason = part.get("reason")
            if not isinstance(reason, str) or not reason:
                return "", session_id, "protocol_malformed_part", 0
            if reason not in (_ANSWER_COMPLETED_REASON, _TOOL_CALLS_REASON):
                return "", session_id, "protocol_incomplete_final", 0
            message["closed"] = True
        elif event_type == "text":
            if message["closed"]:
                # Text after the message was closed is a malformed or hacked
                # stream and must never reach a reply.
                return "", session_id, "protocol_malformed_part", 0
            if part.get("synthetic") or part.get("ignored"):
                continue
            text = part.get("text")
            part_id = part.get("id")
            if not isinstance(text, str) or not isinstance(part_id, str) or not part_id:
                return "", session_id, "protocol_malformed_part", 0
            existing = message["by_id"].get(part_id)
            if existing is not None:
                if existing != text:
                    return "", session_id, "protocol_conflicting_part", 0
                continue
            message["by_id"][part_id] = text
            message["order"].append(part_id)
            text_parts += 1
            last_text_message = message_id
        # step_start, reasoning, and tool_use parts only advance lifecycle.

    if stream_error is not None:
        return "", session_id, stream_error, 0
    if not completion.completed:
        return "", session_id, completion.error_class or "protocol_no_final_message", 0
    if last_text_message is None:
        return "", session_id, "protocol_no_final_message", 0
    # The natively-proven message must be exactly the last message that
    # produced text, and it must be the final message in the stream: a
    # trailing message native state never confirmed must not become the
    # deliverable answer.
    if completion.message_id and last_text_message != completion.message_id:
        return "", session_id, "protocol_ambiguous_final", 0
    if order[-1] != last_text_message:
        return "", session_id, "protocol_ambiguous_final", 0
    final = messages[last_text_message]
    text = "".join(final["by_id"][pid] for pid in final["order"]).strip()
    return text, session_id, None, text_parts


def _extract_marked_answer(text: str) -> tuple[str, str | None]:
    """Return text after the last explicit final-output marker."""
    if FINAL_OUTPUT_MARKER not in text:
        return "", "protocol_missing_final_marker"
    answer = text.rsplit(FINAL_OUTPUT_MARKER, 1)[1].strip()
    if not answer:
        return "", "protocol_empty_final_answer"
    return answer, None


def _failure(
    sha: str,
    error_class: str,
    *,
    code_question: bool = True,
    had_content: bool = False,
) -> Reply:
    """Build the honest failure reply without exposing subprocess diagnostics."""
    return Reply(
        text=failure_text(error_class, code_question=code_question),
        sha=sha,
        ok=False,
        session_id=None,
        error_class=error_class,
        had_content=had_content,
    )


# Review-reference failures the user can fix by naming the repository or the
# full MR/PR URL; the reply must say so instead of a bare class.
_REVIEW_HINT_CLASSES = frozenset({"review_ref_ambiguous", "review_ref_unavailable"})


def _review_failure(error_class: str, sha: str = "none") -> Reply:
    """Build an honest merge-request failure reply without fetch diagnostics.

    Never falls back to auditing the watched checkout, never includes remote
    URLs, stderr, or snapshot paths, and never carries a resumable session.
    """
    if error_class in _REVIEW_HINT_CLASSES:
        text = (
            f"I couldn't resolve that merge request ({error_class}) and I won't guess. "
            "Name the repository or include the full MR/PR URL so I review the right one."
        )
    else:
        text = (
            f"I couldn't verify that merge request ({error_class}) "
            "and I'm not going to guess."
        )
    return Reply(text=text, sha=sha, ok=False, session_id=None, error_class=error_class)


def _review_label(provider: str, number: int) -> str:
    """Return the provider-conventional review label (``MR !188`` / ``PR #188``)."""
    if provider == PROVIDER_GITHUB:
        return f"PR #{number}"
    if provider == PROVIDER_GITLAB:
        return f"MR !{number}"
    return f"review {number}"


def _review_prompt_section(snapshot: ReviewSnapshot) -> str:
    """Render the bounded review context section of the user prompt.

    Contains only validated provider/number metadata, verified full SHAs,
    directory roles, and the bounded changed-path manifest already collected
    by the trusted controller. No remote URLs, credentials, or patch bodies.
    """
    label = _review_label(snapshot.provider, snapshot.number)
    lines = [
        f"  {entry.status} {entry.path}"
        + (f" -> {entry.new_path}" if entry.new_path else "")
        for entry in snapshot.changed_paths
    ]
    manifest = "\n".join(lines) if lines else "  (no changed paths)"
    if snapshot.manifest_truncated:
        manifest += "\n  (changed-path list truncated; additional paths changed)"
    return (
        f"MERGE REQUEST REVIEW: {label}\n"
        f"VERIFIED BASE SHA: {snapshot.base_sha}\n"
        f"VERIFIED HEAD SHA: {snapshot.head_sha}\n"
        f"BASE DIRECTORY (verified merge target, before state): {snapshot.base_dir}\n"
        f"HEAD DIRECTORY (proposed changes, after state): {snapshot.head_dir}\n"
        f"CHANGED PATHS (base -> head, status path):\n{manifest}\n"
        "REVIEW INSTRUCTION: Audit the proposed head changes against the verified "
        "base. Cite concrete file paths from both directories and separate "
        "verified findings from unknowns."
    )


def _external_evidence_section(prepared: PreparedExternal | None) -> str:
    """Render the controller-provided external-evidence prompt section.

    Reviews that also carry an attachment or public-evidence ask keep their
    snapshot provenance AND state the external evidence honestly. The block
    is delimited untrusted data; it never changes what is audited.
    """
    if prepared is None or not prepared.inputs.has_external_content:
        return ""
    return (
        "\nCONTROLLER-PROVIDED EXTERNAL EVIDENCE (UNTRUSTED DATA):\n"
        f"{render_evidence_block(prepared.inputs)}\n"
    )


def _merge_error_class(
    error_class: str | None,
    event_error: str | None,
    completion: NativeCompletion,
) -> str | None:
    """Pick the most honest bounded failure class for one run.

    A stream-level root cause (an in-stream error event such as an auth
    failure, or a protocol violation) outranks the completion gate's bounded
    echo of the same refusal: the gate still refuses the answer either way,
    but the operator learns WHY. When selection merely echoes the completion
    class (or the stream is empty/timed out), the process-level class that
    already describes the run wins.
    """
    if event_error is not None and event_error != completion.error_class:
        return event_error
    return error_class or event_error


TRUNCATION_MARKER = "\n\n[reply truncated]"



def _truncate_cut(text: str, limit: int) -> int:
    """Return the best cut index at or below ``limit``.

    Boundary preference: paragraph (``\n\n``), then sentence (``.``/``!``/
    ``?`` followed by whitespace), then any whitespace; ``limit`` itself only
    when the text contains no boundary at all.

    Args:
        text: Final assistant text that needs truncation.
        limit: Maximum allowed cut index (and the char-level fallback).

    Returns:
        Cut index in ``[0, limit]``.
    """

    paragraph = text.rfind("\n\n", 0, limit)
    if paragraph != -1:
        return paragraph
    for index in range(min(limit - 1, len(text) - 2), -1, -1):
        if text[index] in ".!?" and text[index + 1].isspace():
            return index + 1
    for index in range(min(limit, len(text)) - 1, -1, -1):
        if text[index].isspace():
            return index
    return limit


def _bounded_text(text: str, cap: int) -> str:
    """Apply the configured reply cap without adding metadata.

    Args:
        text: Selected final assistant text.
        cap: Total reply character cap.

    Returns:
        Bounded reply text with a clean truncation boundary when possible.
    """
    if len(text) <= cap:
        return text.strip()
    body_cap = cap - len(TRUNCATION_MARKER)
    if body_cap <= 0:
        return text[:cap]
    cut = _truncate_cut(text, body_cap)
    return f"{text[:cut].rstrip()}{TRUNCATION_MARKER}".strip()


MAX_DOC_ATTACHMENTS = 3
MAX_DOC_ATTACHMENT_BYTES = 512 * 1024
_DOC_ATTACHMENT_SUFFIXES = (".md", ".txt")
_DOC_ATTACHMENT_SECRET_HINTS = (
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer",
    "credentials", "secret", "token", "id_rsa", "id_ed25519",
)


def _collect_doc_attachments(
    roots: list[Path],
    *,
    write_dirs: list[str] | tuple[str, ...] | None,
    since_epoch: float,
) -> tuple[str, ...]:
    """Collect freshly written .md/.txt docs for Discord upload.

    Uses ``git status --porcelain`` plus an mtime check so pre-existing dirty
    files are not uploaded: only docs touched since ``since_epoch`` (the agent
    execution start, with a small tolerance) qualify. Results are bounded to
    ``MAX_DOC_ATTACHMENTS`` files of ``MAX_DOC_ATTACHMENT_BYTES`` each.

    Args:
        roots: Repository worktrees to inspect.
        write_dirs: Normalized repo-relative scopes; empty means anywhere.
        since_epoch: Unix timestamp captured before agent execution.

    Returns:
        Tuple of absolute file paths, sorted, possibly empty. Never raises.
    """
    try:
        scopes = normalize_write_dirs(write_dirs)
    except Exception:  # noqa: BLE001 - fail closed to no attachments
        return ()
    found: list[str] = []
    for root in roots:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain",
                 "--untracked-files=all", "--", "."],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            if len(line) < 4:
                continue
            rel = line[3:].strip()
            if " -> " in rel:  # rename/copy: take the new path
                rel = rel.rsplit(" -> ", 1)[1].strip()
            rel = rel.strip().strip('"')
            if not rel or ".." in rel.split("/"):
                continue
            if not rel.lower().endswith(_DOC_ATTACHMENT_SUFFIXES):
                continue
            lowered = rel.lower()
            if any(hint in lowered for hint in _DOC_ATTACHMENT_SECRET_HINTS):
                continue
            if scopes and not any(
                rel == scope or rel.startswith(scope + "/") for scope in scopes
            ):
                continue
            candidate = (root / rel).resolve()
            try:
                root_real = root.resolve()
                if candidate != root_real and root_real not in candidate.parents:
                    continue
                if not candidate.is_file():
                    continue
                stat = candidate.stat()
                if stat.st_size > MAX_DOC_ATTACHMENT_BYTES:
                    continue
                if stat.st_mtime < since_epoch - 5:
                    continue
            except OSError:
                continue
            found.append(str(candidate))
            if len(found) >= MAX_DOC_ATTACHMENTS:
                return tuple(sorted(set(found)))
    return tuple(sorted(set(found))[:MAX_DOC_ATTACHMENTS])


def _demote_images_without_vision(prepared: PreparedExternal) -> None:
    """Rewrite prepared image records to explicit unsupported statuses.

    Used when ``image_inputs="off"``: native catalog assumptions are not
    detection, so the controller does not hand pixels to the model. Each
    image record becomes an explicit unsupported entry stating that visual
    understanding is unavailable, while independent text and document parts
    still reach the model and no whole job fails merely because an image
    could not be shown.
    """
    rewritten: list[AttachmentInput] = []
    for record in prepared.inputs.attachments:
        if record.kind == "image" and record.status == STATUS_IMAGE:
            rewritten.append(_dc_replace(
                record,
                status=STATUS_UNSUPPORTED,
                limitations=(
                    "not shown: model image capability is not enabled "
                    "(image_inputs=off); visual understanding is unavailable",
                ),
            ))
        else:
            rewritten.append(record)
    prepared.inputs.attachments = rewritten


_SECRET_PATTERN = re.compile(
    r"(?i)(bearer\s+|(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?"
    r"(?:bearer\s+)?)"
    r"[^\s\"',}]+"
)
_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_\-.]{32,}\b")
_FAILURE_DETAIL_CHARS = 600


def _redact_detail(text: str) -> str:
    """Hide credential-shaped values so failure detail is safe to log."""
    text = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}<hidden>", text)
    text = _LONG_TOKEN_PATTERN.sub("<hidden>", text)
    text = " ".join(text.split())
    return text[:_FAILURE_DETAIL_CHARS]


def _log_failure_detail(
    label: str, error_class: str, stderr_sample: str, events: list[dict[str, Any]]
) -> None:
    """Log OpenCode's own error text (stderr and error events), redacted."""
    event_errors = []
    for event in events:
        if event.get("type") != "error":
            continue
        err = event.get("error")
        if isinstance(err, dict):
            event_errors.append(f"{err.get('type', '')}: {err.get('message', '')}")
        else:
            event_errors.append(str(err))
    logger.warning(
        "[%s] failure error=%s event_errors=%s stderr=%s",
        label,
        error_class,
        _redact_detail(" | ".join(event_errors)) or "none",
        _redact_detail(stderr_sample) or "none",
    )


class OpenCodeRunner:
    """Run the configured native OI OpenCode agent for one conversation."""

    def __init__(self, cfg: Config, store: Store | None = None) -> None:
        """Create a runner using the same config and isolated XDG resolver."""
        self._cfg = cfg
        self._store = store
        self._answer_evidence: PreparedExternal | None = None
        self._answer_code_question = True
        self._format_reminder: str | None = None

    def _take_answer_overrides(
        self,
    ) -> tuple[PreparedExternal | None, bool, str | None]:
        """Consume one-shot answer-run overrides set by the watcher."""
        evidence = self._answer_evidence
        code_question = self._answer_code_question
        reminder = self._format_reminder
        self._answer_evidence = None
        self._answer_code_question = True
        self._format_reminder = None
        return evidence, code_question, reminder

    async def run(
        self,
        target: WatchTarget,
        author: str,
        question: str,
        excerpt: str,
        session_id: str | None = None,
        member_id: str | None = None,
        bot_name: str = "",
        bot_id: int | None = None,
    ) -> Reply:
        """Refresh, snapshot, execute, and provenance-stamp one or more repository audits.

        When the latest question carries an explicit MR/PR reference, the run
        becomes a review of controller-fetched base/head snapshots instead of
        the watched checkout. Unresolvable or ambiguous references fail
        honestly; they never fall back to auditing the current branch.
        Args:
            target: Watched repository/guild binding.
            author: Discord display name.
            question: Latest user question.
            excerpt: Bounded Discord conversation excerpt.
            session_id: Optional OpenCode session to resume (ignored for
                MR/PR reviews, which never share branch-audit context).
            member_id: Optional author member id; when known, memory rendering
                is restricted to current participants (plan Phase 3.6).
            bot_name: Discord display name the agent posts under; grounds the
                prompt identity block.
            bot_id: Stable Discord user id of the agent's bot account.

        Returns:
            Bounded Reply. OpenCode/process failures have ``ok=False``.
        """
        repo_paths = target.repo_paths
        primary_root = Path(target.primary_repo).expanduser().resolve()
        additional_roots = [
            Path(p).expanduser().resolve()
            for p in repo_paths
            if str(Path(p).expanduser().resolve()) != str(primary_root)
        ]

        async with await _acquire_repo_locks(repo_paths):
            evidence, code_question, reminder = self._take_answer_overrides()
            try:
                # Network-probing resolution runs off the event loop; it only
                # probes configured repositories for fixed provider refs.
                review_target = await asyncio.to_thread(
                    resolve_review_target, question, repo_paths
                )
            except ReviewRefError as exc:
                logger.warning(
                    "[opencode] review refused class=%s", exc.failure_class
                )
                return _review_failure(exc.failure_class)
            if review_target is not None:
                if evidence is not None:
                    return await self._run_review_locked(
                        review_target, author, question, excerpt,
                        bot_name=bot_name, bot_id=bot_id, prepared=evidence,
                    )
                return await self._run_review_locked(
                    review_target, author, question, excerpt,
                    bot_name=bot_name, bot_id=bot_id,
                )
            return await self._run_locked(
                primary_root,
                additional_roots,
                target,
                author,
                question,
                excerpt,
                session_id,
                member_id,
                bot_name=bot_name,
                bot_id=bot_id,
                evidence=evidence,
                code_question=code_question,
                format_reminder=reminder,
            )
    async def run_raw_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        timeout: int = 60,
        steps: int = 1,
    ) -> str | None:
        """Execute a raw model query with no tools at all.

        The interpretation agent runs under the native v2 contract with every
        action denied, so it has no repository, network, or filesystem
        authority. The answer is returned only when native state proves the
        reported message actually completed.

        Args:
            system_prompt: Optional override for the default JSON instruction.
            timeout: Wall-clock bound in seconds.
            steps: Agent step budget. One step suffices for a direct JSON
                answer; callers that may need a step after a denied tool
                attempt (the web classifier) pass a larger bound.

        Raises:
            ValueError: When the prompt exceeds the bounded stdin limit.
        """
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_PROMPT_BYTES:
            logger.warning(
                "[opencode] raw prompt rejected class=prompt_too_large bytes=%d",
                len(prompt_bytes),
            )
            raise ValueError("prompt exceeds the bounded stdin limit")
        paths = resolve_opencode_paths()
        paths.ensure()
        try:
            binary = resolve_runtime_binary(self._cfg.opencode_binary, paths)
        except Exception:  # noqa: BLE001 - config/bootstrap boundary
            return None

        agent_document = build_tool_free_agent(
            self._cfg.opencode_model,
            system_prompt or "You are a helpful JSON analysis agent. Output valid JSON.",
            steps=steps,
        )
        argv = [binary, *build_argv(self._cfg.opencode_model, "oi_eval")]
        environment = paths.environment()
        environment["OPENCODE_CONFIG_CONTENT"] = config_content(agent_document)
        db_path = native_db_path(paths.data_home)

        process = None
        stdout_task = None
        stderr_task = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                # The interpretation agent has no repository binding: its
                # working directory is the isolated native home, never an
                # operator directory.
                cwd=str(paths.home),
                start_new_session=(os.name == "posix"),
            )
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(_consume_jsonl(process.stdout))
            stderr_task = asyncio.create_task(_consume_stderr(process.stderr))
            await asyncio.wait_for(
                asyncio.gather(process.wait(), stdout_task, stderr_task),
                timeout=timeout,
            )
            events, _ = stdout_task.result()
            session_id, message_id, had_error = _stream_identity(events)
            completion = verify_completion(
                db_path, session_id, message_id,
                returncode=process.returncode, has_error_event=had_error,
            )
            text, _, selection_error, _ = _select_final_text_v2(events, completion)
            if selection_error is None and text:
                return text
            return None
        except asyncio.CancelledError:
            # Cancellation must not orphan the start_new_session process group.
            if process is not None:
                await _terminate_process(process)
            raise
        except Exception as exc:  # noqa: BLE001 - subprocess/model boundary
            logger.warning(
                "[opencode] run_raw_prompt failed: %s", type(exc).__name__
            )
            if process is not None:
                await _terminate_process(process)
            return None
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()

    async def classify_web_request(self, message: str) -> str | None:
        """Classify one request with the configured model and no tools.

        Every action is denied, so a denied tool attempt consumes a step;
        steps=3 leaves the agent room to answer with the required JSON after
        such an attempt instead of emitting step-limit narration.
        """
        try:
            return await self.run_raw_prompt(
                message,
                system_prompt=CLASSIFIER_SYSTEM,
                timeout=30,
                steps=3,
            )
        except ValueError:
            return None

    async def run_web_research(
        self,
        query: str,
        urls: list[str],
        *,
        max_sources: int,
        timeout: int | None = None,
    ) -> ResearchResult:
        """Run public web research in an empty directory and an isolated home.

        The permission policy allows only websearch and webfetch. No repository,
        channel history, or OI secret environment variable is supplied. Every
        listed public URL is fetched; the step and time budget grow with that
        list. A caller-supplied timeout is kept; otherwise it is 180 seconds
        plus 60 seconds per URL.
        """
        try:
            binary = resolve_runtime_binary(
                self._cfg.opencode_binary, resolve_opencode_paths()
            )
        except Exception:  # noqa: BLE001 - config/bootstrap boundary
            return ResearchResult(False, category="binary_unavailable")
        link_lines = "\n".join(f"- {url}" for url in urls) or "(none)"
        fetch_rule = (
            "Webfetch every listed URL, one by one, then answer. "
            "List each retrieved URL in sources. "
            "A URL that fails is reported as failed in the summary, not skipped silently."
        )
        prompt = (
            f"QUESTION:\n{query.strip()}\n\n"
            "PUBLIC URLS FROM THE REQUEST AND RECENT CHANNEL MESSAGES:\n"
            f"{link_lines}\n\n"
            f"{fetch_rule}\n"
        )
        system = (
            "You research public web pages. Use only websearch and webfetch. "
            f"{fetch_rule} "
            "Do not read files, edit, or run commands. "
            "Output one JSON object and nothing else: "
            '{"summary":"factual summary","sources":["https://..."]}. '
            "Every source must be a URL you actually retrieved. Never invent one."
        )
        if timeout is None:
            timeout = 180 + 60 * len(urls)
        steps = max(1, int(self._cfg.opencode_steps)) + len(urls)
        agent_document = build_research_agent(
            self._cfg.opencode_model, system, steps=steps
        )
        cwd = new_empty_research_dir()
        disposable = DisposableNativeRoot()
        try:
            target_db = await initialize_native_schema(binary, disposable)
            copied = copy_isolated_credentials(
                native_db_path(resolve_opencode_paths().data_home), target_db
            )
            if copied == 0:
                return ResearchResult(False, category="no_isolated_credentials")
            copy_model_catalog(
                native_db_path(resolve_opencode_paths().data_home), target_db
            )
            environment = disposable.environment()
            environment["OPENCODE_CONFIG_CONTENT"] = config_content(agent_document)
            argv = [binary, *build_argv(self._cfg.opencode_model, "oi_research")]
            text, error_class = await self._research_process(
                argv, prompt, environment, cwd, disposable, timeout=timeout
            )
            if error_class or not text:
                return ResearchResult(False, category=error_class or "empty_response")
            return parse_research_output(
                text,
                max_chars=6000,
                max_sources=max_sources,
            )
        finally:
            disposable.close()
            import shutil
            shutil.rmtree(cwd, ignore_errors=True)

    async def _research_process(
        self,
        argv: list[str],
        prompt: str,
        environment: dict[str, str],
        cwd: Path,
        disposable: DisposableNativeRoot,
        *,
        timeout: int,
    ) -> tuple[str, str | None]:
        """Spawn one research process and return its completed text."""
        process = None
        stdout_task = None
        stderr_task = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=str(cwd),
                start_new_session=(os.name == "posix"),
            )
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(_consume_jsonl(process.stdout))
            stderr_task = asyncio.create_task(_consume_stderr(process.stderr))
            await asyncio.wait_for(
                asyncio.gather(process.wait(), stdout_task, stderr_task),
                timeout=timeout,
            )
            events, _ = stdout_task.result()
            session_id, message_id, had_error = _stream_identity(events)
            completion = verify_completion(
                native_db_path(disposable.data_home),
                session_id,
                message_id,
                returncode=process.returncode,
                has_error_event=had_error,
            )
            text, _, selection_error, _ = _select_final_text_v2(events, completion)
            if selection_error:
                return "", selection_error
            return text, None
        except asyncio.TimeoutError:
            if process is not None:
                await _terminate_process(process)
            return "", "timeout"
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process(process)
            raise
        except Exception as exc:  # noqa: BLE001 - subprocess boundary
            logger.warning("[research] process failed: %s", type(exc).__name__)
            if process is not None:
                await _terminate_process(process)
            return "", "process_error"
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()

    async def infer_context_request(self, question: str) -> ContextRequest | None:
        """Interpret an explicit history ask locally, then use one LLM fallback.

        Literal dates, counts, and relative windows are deterministic and do
        not depend on a second provider call. Less literal expansion asks may
        use the bounded JSON fallback; any fallback failure returns None.
        """
        direct_request = parse_explicit_request(question)
        if direct_request is not None:
            logger.info(
                "[opencode] context expansion deterministic count=%s since_days=%s since_date=%s",
                direct_request.count,
                direct_request.since_days,
                direct_request.since_date,
            )
            return direct_request
        if not mentions_expansion(question):
            return None
        try:
            text = await self.run_raw_prompt(
                _context_spec_prompt() + question.strip(),
                timeout=45,
            )
        except ValueError:
            return None
        request = parse_spec(text)
        if request is None or request.is_default:
            return None
        logger.info(
            "[opencode] context expansion spec count=%s since_days=%s since_date=%s",
            request.count,
            request.since_days,
            request.since_date,
        )
        return request

    def _vision_attachments(self, prepared: PreparedExternal) -> list[Path]:
        """Return native image paths under the validated capability setting.

        ``image_inputs="on"`` attaches the prepared image files through the
        documented ``--file`` flags so the model receives real pixels.
        ``image_inputs="off"`` (the conservative default) attaches nothing:
        the image records are demoted to explicit unsupported statuses so the
        prompt honestly states visual understanding is unavailable, and the
        run still answers independent text and document parts.
        """
        preparation = prepared.image_preparation
        if preparation is None or not preparation.images:
            return []
        if self._cfg.image_inputs != "on":
            _demote_images_without_vision(prepared)
            return []
        return list(preparation.paths)

    async def run_external_evidence(
        self,
        prepared: PreparedExternal,
        *,
        target: WatchTarget,
        author: str,
        question: str,
        excerpt: str,
        bot_name: str = "",
        bot_id: int | None = None,
    ) -> Reply:
        """Answer an external-evidence question in a disposable read-only run.

        External runs differ from ordinary audits in five structural ways, all
        of which are properties of this method rather than of the prompt:

        * **Read-only regardless of ``write_mode``.** External content is
          untrusted; a run that carries it never receives a docs-only grant.
        * **Fresh disposable native storage.** HOME, XDG config/data/state/
          cache, and TMPDIR are private and deleted afterwards, because the
          native harness persists prompt text and attachment bytes. No
          conversation session is resumed and no session id is returned, so
          nothing is ever stored under a conversation key.
        * **Minimal credential material only.** The already-isolated OI native
          database's credential rows are copied into the private root; no
          session, message, project, or permission row is copied, no personal
          auth is read, and no permission rule grants the model access to
          those paths.
        * **Real images.** Prepared image paths are attached through the
          documented native ``--file`` flag, so the model receives pixels.
        * **Honest limits.** Unsupported inputs, clipped evidence, and
          retrieval refusals are stated in the prompt so the model reports the
          real limitation instead of inventing content.

        Args:
            prepared: Controller-prepared evidence and attachments.
            root: Repository used as the native working directory.
            author: Discord display name.
            question: Latest user question.
            excerpt: Bounded Discord conversation excerpt.
            bot_name: Discord display name the agent posts under.
            bot_id: Stable Discord user id of the bot account.

        Returns:
            A bounded Reply. External runs never carry a resumable session.
        """
        inputs = prepared.inputs
        if not inputs.has_external_content:
            return _failure("none", "no_external_content")
        try:
            binary = resolve_runtime_binary(self._cfg.opencode_binary, resolve_opencode_paths())
        except Exception:  # noqa: BLE001 - config/bootstrap boundary
            return _failure("none", "binary_unavailable")
        # An external-evidence run must NOT skip review-reference resolution.
        # Bypassing it would let an attachment or news question that happens
        # to contain an MR/PR number silently audit the current checkout and
        # report "none" provenance. Unresolvable references fail honestly
        # exactly as an ordinary run does, and a resolved reference is audited
        # against controller-fetched base/head snapshots with MR provenance.
        repo_paths = target.repo_paths
        try:
            review_target = await asyncio.to_thread(
                resolve_review_target, question, repo_paths
            )
        except ReviewRefError as exc:
            logger.warning(
                "[external] review refused class=%s", exc.failure_class
            )
            return _review_failure(exc.failure_class)
        if review_target is not None:
            return await self._run_review_locked(
                review_target, author, question, excerpt,
                bot_name=bot_name, bot_id=bot_id, prepared=prepared,
            )
        root = Path(target.primary_repo).expanduser().resolve()
        additional_roots = [
            Path(p).expanduser().resolve()
            for p in repo_paths
            if str(Path(p).expanduser().resolve()) != str(root)
        ]

        attachment_files = self._vision_attachments(prepared)
        evidence_block = render_evidence_block(inputs)
        prompt = (
            f"From: {author}\n\n"
            f"THREAD CONTEXT:\n{excerpt or '(start of conversation)'}\n\n"
            f"CONTROLLER-PROVIDED EXTERNAL EVIDENCE (UNTRUSTED DATA):\n{evidence_block}\n\n"
            f"LATEST QUESTION:\n{question}"
        )
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_PROMPT_BYTES:
            logger.warning(
                "[external] prompt rejected class=prompt_too_large bytes=%d",
                len(prompt_bytes),
            )
            return _failure("none", "prompt_too_large")

        # An external run is always read-only, whatever write_mode says.
        prompt_str = build_prompt(
            self._cfg.personality,
            primary_repo=str(root),
            additional_repos=[],
            environment_mode=self._cfg.resolved_environment_mode,
            memory=None,
            bot_name=bot_name,
            bot_id=bot_id,
            write_mode="read-only",
            write_dirs=[],
            external_inputs=True,
        )

        disposable = DisposableNativeRoot()
        try:
            target_db = await initialize_native_schema(binary, disposable)
            copied = copy_isolated_credentials(
                native_db_path(resolve_opencode_paths().data_home), target_db
            )
            if copied == 0:
                return _failure("none", "no_isolated_credentials")
            (
                events, stderr_bytes, stderr_sample, returncode,
                error_class, duration_ms, completion,
            ) = await self._execute_agent(
                binary=binary,
                root=root,
                additional_roots=additional_roots,
                prompt_str=prompt_str,
                prompt=prompt,
                session_id=None,
                write_mode="read-only",
                write_dirs=[],
                files=attachment_files,
                disposable=disposable,
            )
            text, _session, event_error, text_parts = _select_final_text_v2(
                events, completion
            )
            error_class = _merge_error_class(
                error_class, event_error, completion
            )
            if error_class is None:
                text, marker_error = _extract_marked_answer(text)
                error_class = marker_error
            if error_class is None and not text:
                error_class = "empty_response"
            logger.info(
                "[external] events=%d text_parts=%d stderr_bytes=%d duration_ms=%d "
                "exit=%s images=%d evidence=%d error=%s",
                len(events), text_parts, stderr_bytes, duration_ms,
                returncode if returncode is not None else "none",
                len(attachment_files), len(inputs.evidence),
                error_class or "none",
            )
            if error_class:
                _log_failure_detail("external", error_class, stderr_sample, events)
                return _failure("none", error_class)
            return Reply(
                text=_bounded_text(text, self._cfg.max_reply_chars),
                sha="none",
                ok=True,
                session_id=None,
            )
        finally:
            # Guaranteed cleanup on success, failure, timeout, and cancellation:
            # no native session, no prompt text, and no attachment bytes survive.
            disposable.close()


    async def _run_locked(
        self,
        root: Path,
        additional_roots: list[Path],
        target: WatchTarget,
        author: str,
        question: str,
        excerpt: str,
        session_id: str | None,
        member_id: str | None = None,
        bot_name: str = "",
        bot_id: int | None = None,
        evidence: PreparedExternal | None = None,
        code_question: bool = True,
        format_reminder: str | None = None,
    ) -> Reply:
        """Execute one audit while holding repository serialization locks."""
        all_roots = [root, *additional_roots]
        freshness: list[str] = []
        repo_states: list[dict[str, Any]] = []

        for r in all_roots:
            pulled = await asyncio.to_thread(git_pull, str(r))
            r_sha = audit_sha(r)
            fp_before = worktree_fingerprint(r)
            repo_states.append({
                "root": r,
                "name": r.name,
                "sha_before": r_sha,
                "fp_before": fp_before,
                "pulled": pulled,
            })
            if not pulled:
                freshness.append(f"git pull failed for {r.name} — code may be behind remote")
            if r_sha.endswith("-dirty"):
                freshness.append(f"worktree {r.name} has uncommitted changes")

        initial_shas = [
            f"{st['name']}@{st['sha_before']}" if len(all_roots) > 1 else st["sha_before"]
            for st in repo_states
        ]
        combined_sha_prompt = ", ".join(initial_shas)

        freshness_warning = (
            f"FRESHNESS WARNING: {'; '.join(freshness)}.\n" if freshness else ""
        )
        prompt = (
            f"From: {author}\n\n"
            f"AUDITED SHA: {combined_sha_prompt}\n"
            f"{freshness_warning}"
            f"THREAD CONTEXT:\n{excerpt or '(start of conversation)'}\n\n"
            f"LATEST QUESTION:\n{question}"
        )
        if evidence is not None and evidence.inputs.has_external_content:
            prompt += _external_evidence_section(evidence)
        if format_reminder:
            prompt += f"\n\nFORMAT REMINDER:\n{format_reminder}\n"

        try:
            binary = resolve_runtime_binary(self._cfg.opencode_binary, resolve_opencode_paths())
        except Exception:  # noqa: BLE001 - config/bootstrap boundary
            return _failure(combined_sha_prompt, "binary_unavailable")
        # Bounded stdin (finding B10): fail closed before spawning anything.
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > MAX_PROMPT_BYTES:
            logger.warning(
                "[opencode] prompt rejected class=prompt_too_large bytes=%d",
                len(prompt_bytes),
            )
            return _failure(combined_sha_prompt, "prompt_too_large")
        snapshot = None
        scope_id = scope_id_for_target(target)
        if self._store is not None and scope_id:
            try:
                snapshot = self._store.memory_snapshot(
                    "discord",
                    scope_id,
                    participant_member_ids=(
                        [str(member_id)] if member_id else None
                    ),
                    max_profiles=10,
                )
            except Exception as exc:  # noqa: BLE001 - memory is best-effort
                logger.debug(
                    "[runner] could not fetch memory snapshot: %s",
                    type(exc).__name__,
                )

        write_mode = getattr(self._cfg, "write_mode", "read-only")
        write_dirs = list(getattr(self._cfg, "write_dirs", []) or [])
        prompt_str = build_prompt(
            self._cfg.personality,
            primary_repo=str(root),
            additional_repos=[str(r) for r in additional_roots],
            environment_mode=self._cfg.resolved_environment_mode,
            memory=snapshot,
            bot_name=bot_name,
            bot_id=bot_id,
            write_mode=write_mode,
            write_dirs=write_dirs,
            external_inputs=evidence is not None and evidence.inputs.has_external_content,
        )

        run_started = time.time()
        (
            events, stderr_bytes, stderr_sample, returncode,
            error_class, duration_ms, completion,
        ) = (
            await self._execute_agent(
                binary=binary,
                root=root,
                additional_roots=additional_roots,
                prompt_str=prompt_str,
                prompt=prompt,
                session_id=session_id,
                write_mode=write_mode,
                write_dirs=write_dirs,
            )
        )
        text, returned_session, event_error, text_parts = _select_final_text_v2(
            events, completion
        )
        error_class = _merge_error_class(
            error_class, event_error, completion
        )
        raw_text = text
        if error_class is None:
            text, marker_error = _extract_marked_answer(text)
            error_class = marker_error
        had_content = bool(raw_text.strip()) and error_class == "protocol_missing_final_marker"
        if error_class is None and not text:
            error_class = "empty_response"

        final_sha_list = []
        for st in repo_states:
            r = st["root"]
            r_name = st["name"]
            r_sha = st["sha_before"]
            warning = mutation_warning(r, st["fp_before"])
            if warning:
                if not r_sha.endswith("-dirty"):
                    r_sha = f"{r_sha}-dirty"
                logger.warning("[opencode] %s in %s", warning, r_name)
            final_sha_list.append(f"{r_name}@{r_sha}" if len(all_roots) > 1 else r_sha)

        final_combined_sha = ", ".join(final_sha_list)

        logger.info(
            "[opencode] events=%d text_parts=%d stderr_bytes=%d duration_ms=%d "
            "exit=%s model=%s session=%s error=%s",
            len(events), text_parts, stderr_bytes, duration_ms,
            returncode if returncode is not None else "none",
            self._cfg.opencode_model,
            (returned_session or "")[:8] or "none",
            error_class or "none",
        )
        if error_class:
            _log_failure_detail("opencode", error_class, stderr_sample, events)
            return _failure(
                final_combined_sha, error_class,
                code_question=code_question, had_content=had_content,
            )
        attachments: tuple[str, ...] = ()
        if write_mode == "docs-only":
            attachments = _collect_doc_attachments(
                [root, *additional_roots],
                write_dirs=write_dirs,
                since_epoch=run_started,
            )
        return Reply(
            text=_bounded_text(text, self._cfg.max_reply_chars),
            sha=final_combined_sha,
            ok=True,
            session_id=returned_session,
            attachments=attachments,
        )

    async def _execute_agent(
        self,
        *,
        binary: str,
        root: Path,
        additional_roots: list[Path],
        prompt_str: str,
        prompt: str,
        session_id: str | None,
        write_mode: str = "read-only",
        write_dirs: list[str] | tuple[str, ...] | None = None,
        files: list[Path] | None = None,
        disposable: DisposableNativeRoot | None = None,
        controller_snapshots: bool = False,
    ) -> tuple[
        list[dict[str, Any]], int, str, int | None, str | None, int, NativeCompletion
    ]:
        """Spawn one bounded native v2 audit subprocess and collect telemetry.

        The repository Location is the subprocess working directory: native v2
        removed ``--dir``. ``--standalone`` runs a private server so the run
        never attaches to the shared background service. The agent config
        grants only the fail-closed v2 permission rules for ``root`` plus
        explicitly authorized ``additional_roots``; every other action stays
        denied unless ``write_mode="docs-only"`` grants .md/.txt ``edit``
        (policy.py is the single source of that rule list). Reviews always run
        read-only.

        Args:
            binary: Resolved OpenCode executable path.
            root: Primary repository, used as the native working directory.
            additional_roots: Auxiliary roots authorized read-only.
            prompt_str: Embedded system prompt.
            prompt: Bounded user prompt written to stdin.
            session_id: Optional OpenCode session to resume.
            write_mode: ``"read-only"`` or ``"docs-only"``.
            write_dirs: Optional repo-relative scopes for doc writes.
            files: Optional image paths attached as real native attachments.
            disposable: Private native root for external-evidence runs. When
                supplied, the run never touches the ordinary conversation
                storage and the caller deletes the root afterwards.
            controller_snapshots: True only for review runs, whose roots are
                controller-materialized verified-SHA snapshots (see
                :func:`build_permission_policy`).

        Returns:
            ``(events, stderr_bytes, stderr_sample, returncode, error_class,
            duration_ms)``. ``error_class`` covers spawn, timeout, protocol,
            and exit failures; final-text selection stays with the caller.
        """
        agent_document = build_agent_config(
            self._cfg.opencode_model,
            self._cfg.opencode_steps,
            root,
            prompt_str,
            additional_repo_roots=additional_roots,
            write_mode=write_mode,
            write_dirs=write_dirs,
            controller_snapshots=controller_snapshots,
        )
        argv = [
            binary,
            *build_argv(
                self._cfg.opencode_model, "oi",
                session_id=session_id, files=files,
            ),
        ]
        if disposable is not None:
            environment = disposable.environment()
            db_path = native_db_path(disposable.data_home)
        else:
            paths = resolve_opencode_paths()
            paths.ensure()
            environment = paths.environment()
            db_path = native_db_path(paths.data_home)
        environment["OPENCODE_CONFIG_CONTENT"] = config_content(agent_document)
        started = time.monotonic()
        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task | None = None
        stderr_task: asyncio.Task | None = None
        events: list[dict[str, Any]] = []
        stderr_bytes = 0
        stderr_sample = ""
        error_class: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                cwd=str(root.resolve()),
                start_new_session=(os.name == "posix"),
            )
            assert process.stdin is not None
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            assert process.stdout is not None and process.stderr is not None
            stdout_task = asyncio.create_task(_consume_jsonl(process.stdout))
            stderr_task = asyncio.create_task(_consume_stderr(process.stderr))
            await asyncio.wait_for(
                asyncio.gather(process.wait(), stdout_task, stderr_task),
                timeout=self._cfg.opencode_timeout_seconds,
            )
            events, _ = stdout_task.result()
            stderr_bytes, stderr_sample = stderr_task.result()
        except asyncio.TimeoutError:
            error_class = "timeout"
            if process is not None:
                await _terminate_process(process)
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process(process)
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
            raise
        except _ProtocolError as exc:
            error_class = exc.error_class
            if process is not None:
                await _terminate_process(process)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("[opencode] process boundary failed: %s", type(exc).__name__)
            if process is not None:
                await _terminate_process(process)
        finally:
            for task in (stdout_task, stderr_task):
                if task is not None and not task.done():
                    task.cancel()
        if process is None:
            error_class = error_class or "process_error"
        elif process.returncode not in (0, None):
            stderr_lower = stderr_sample.lower()
            if "session" in stderr_lower and any(
                w in stderr_lower for w in ("not found", "unknown", "deleted", "invalid")
            ):
                error_class = "unknown_session"
            else:
                error_class = error_class or "nonzero_exit"
        completion = NativeCompletion(False, "empty_response")
        if not events:
            error_class = error_class or "empty_response"
        else:
            seen_session, message_id, had_error = _stream_identity(events)
            completion = verify_completion(
                db_path,
                seen_session,
                message_id,
                returncode=process.returncode if process is not None else None,
                has_error_event=had_error,
            )
            if not completion.completed:
                error_class = error_class or completion.error_class
        duration_ms = int((time.monotonic() - started) * 1000)
        returncode = process.returncode if process is not None else None
        return (
            events, stderr_bytes, stderr_sample, returncode,
            error_class, duration_ms, completion,
        )

    async def _run_review_locked(
        self,
        review_target: ReviewTarget,
        author: str,
        question: str,
        excerpt: str,
        bot_name: str = "",
        bot_id: int | None = None,
        prepared: PreparedExternal | None = None,
    ) -> Reply:
        """Execute one MR/PR audit against controller-owned base/head snapshots.

        The watched checkout is neither refreshed nor audited: the review
        subject is the immutable snapshot pair the trusted controller fetched
        read-only from the configured origin. Only the head snapshot (the
        subprocess working directory) and the base snapshot (explicit
        auxiliary root) are authorized; other configured repositories are
        excluded from review sessions. No OpenCode session is resumed and
        none is returned, so review runs cannot reuse or store conversation
        branch-audit context.

        When ``prepared`` carries external evidence (an attachment or news ask
        that also references this MR/PR), the run stays a real snapshot
        review: the controller-rendered evidence rides along as clearly
        delimited untrusted data, and the run gets the same disposable native
        storage as any external run because the harness persists prompt text
        and attachment bytes.

        Args:
            review_target: Reference resolved against configured repositories.
            author: Discord display name.
            question: Latest user question.
            excerpt: Bounded Discord conversation excerpt.
            bot_name: Discord display name the agent posts under.
            bot_id: Stable Discord user id of the agent's bot account.
            prepared: Optional controller-prepared external evidence.

        Returns:
            Bounded Reply whose provenance is the MR form
            ``MR !188 base <sha> head <sha>`` (or the GitHub ``PR #188`` form).
        """
        provider = review_target.provider
        number = review_target.number
        try:
            # Controller-side fetch/archive/diff is synchronous networked Git;
            # run it off the event loop while holding the repository locks.
            review = await asyncio.to_thread(
                materialized_review, review_target.repo_path, provider, number
            )
        except ReviewSnapshotError as exc:
            logger.warning(
                "[opencode] review provider=%s number=%d class=%s",
                provider, number, exc.failure_class,
            )
            return _review_failure(exc.failure_class)
        try:
            snapshot = review.snapshot
            provenance = (
                f"{_review_label(provider, number)} "
                f"base {snapshot.base_sha} head {snapshot.head_sha}"
            )
            review_attachment_files: list[Path] = []
            if prepared is not None and prepared.inputs.has_external_content:
                review_attachment_files = self._vision_attachments(prepared)
            prompt = (
                f"From: {author}\n\n"
                f"{_review_prompt_section(snapshot)}\n"
                f"AUDITED SHA: {provenance}\n"
                f"{_external_evidence_section(prepared)}"
                f"THREAD CONTEXT:\n{excerpt or '(start of conversation)'}\n\n"
                f"LATEST QUESTION:\n{question}"
            )
            try:
                binary = resolve_runtime_binary(self._cfg.opencode_binary, resolve_opencode_paths())
            except Exception:  # noqa: BLE001 - config/bootstrap boundary
                return _review_failure("binary_unavailable", sha=provenance)
            # Bounded stdin: fail closed before spawning anything.
            prompt_bytes = prompt.encode("utf-8")
            if len(prompt_bytes) > MAX_PROMPT_BYTES:
                logger.warning(
                    "[opencode] review prompt rejected class=prompt_too_large bytes=%d",
                    len(prompt_bytes),
                )
                return _review_failure("prompt_too_large", sha=provenance)
            # Memory is intentionally omitted: reviews are isolated one-shot
            # audits and the snapshot section must keep the prompt bounded.
            prompt_str = build_prompt(
                self._cfg.personality,
                primary_repo=str(snapshot.head_dir),
                additional_repos=[str(snapshot.base_dir)],
                environment_mode=self._cfg.resolved_environment_mode,
                bot_name=bot_name,
                bot_id=bot_id,
                external_inputs=prepared is not None,
            )
            # An external-evidence review is still an external run: native
            # storage would otherwise persist the prompt text and any
            # attachment bytes under ordinary conversation state, so it gets
            # the same disposable root and credential copy as
            # run_external_evidence, and it is deleted in the finally below.
            disposable: DisposableNativeRoot | None = None
            files: list[Path] = []
            if prepared is not None:
                disposable = DisposableNativeRoot()
                try:
                    target_db = await initialize_native_schema(binary, disposable)
                    copied = copy_isolated_credentials(
                        native_db_path(resolve_opencode_paths().data_home), target_db
                    )
                except BaseException:
                    disposable.close()
                    raise
                if copied == 0:
                    disposable.close()
                    return _review_failure("no_isolated_credentials", sha=provenance)
                files = review_attachment_files
            try:
                (
                    events, stderr_bytes, stderr_sample, returncode,
                    error_class, duration_ms, completion,
                ) = (
                    await self._execute_agent(
                        binary=binary,
                        root=snapshot.head_dir,
                        additional_roots=[snapshot.base_dir],
                        prompt_str=prompt_str,
                        prompt=prompt,
                        session_id=None,
                        files=files,
                        disposable=disposable,
                        controller_snapshots=True,
                    )
                )
            finally:
                if disposable is not None:
                    disposable.close()
            text, _, event_error, text_parts = _select_final_text_v2(events, completion)
            error_class = _merge_error_class(
                error_class, event_error, completion
            )
            if error_class is None:
                text, marker_error = _extract_marked_answer(text)
                error_class = marker_error
            if error_class is None and not text:
                error_class = "empty_response"
            logger.info(
                "[opencode] review provider=%s number=%d events=%d text_parts=%d "
                "stderr_bytes=%d duration_ms=%d exit=%s model=%s error=%s",
                provider, number, len(events), text_parts, stderr_bytes,
                duration_ms, returncode if returncode is not None else "none",
                self._cfg.opencode_model, error_class or "none",
            )
            if error_class:
                _log_failure_detail("opencode review", error_class, stderr_sample, events)
                return _review_failure(error_class, sha=provenance)
            # Provenance remains in the structured Reply.sha field, not chat text.
            return Reply(
                text=_bounded_text(text, self._cfg.max_reply_chars),
                sha=provenance,
                ok=True,
                session_id=None,
            )
        finally:
            # Guaranteed cleanup on success, failure, timeout, and cancellation.
            review.close()
