"""Tool-free single-prompt calls through omp, isolated in a private home.

Raw prompts (classifiers, summaries) run here tool-free. Repository replies and
research run through omp's agent path when ``harness = "omp"``. Every raw call runs with ``--no-tools`` and no session,
extensions, skills, rules, or context files, so the model has no authority.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..config import _resolve_binary
from .native import NativeCompletion
from .omp_policy import mode_tools
from .paths import OpenCodePaths, resolve_opencode_paths
from .runner import (
    _consume_jsonl,
    _consume_stderr,
    _provider_class,
    _ProtocolError,
    _terminate_process,
)

logger = logging.getLogger(__name__)


def omp_argv(binary: str, model: str, system_prompt: str, max_time: int) -> list[str]:
    """Return the argv for one tool-free omp print-mode call; the prompt goes on stdin."""
    return [
        binary, "-p", "--mode", "json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--max-time", str(max_time),
        "--no-tools", "--no-session", "--no-extensions", "--no-skills",
        "--no-rules", "--no-context-files", "--no-title", "--no-lsp",
    ]


def omp_model_selectors(binary: str, paths: OpenCodePaths) -> set[str] | None:
    """Return the model selectors omp lists under the private environment.

    ``omp models --json`` only lists models and makes no model call. Returns
    None when the binary is missing, exits nonzero, or prints unparsable output.
    """
    resolved = _resolve_binary(binary)
    if resolved is None:
        return None
    paths.ensure()
    try:
        proc = subprocess.run(
            [resolved, "models", "--json"],
            env=paths.environment(), cwd=str(paths.home),
            capture_output=True, text=True, timeout=60, check=False,
        )
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if proc.returncode != 0 or not isinstance(data, dict):
        return None
    models = data.get("models")
    if not isinstance(models, list):
        return None
    return {item["selector"] for item in models
            if isinstance(item, dict) and isinstance(item.get("selector"), str)}


def _final_answer(events: list[dict[str, Any]]) -> str | None:
    """Return the assistant text only when the last assistant message stopped normally."""
    ended = [
        event["message"] for event in events
        if event.get("type") == "message_end"
        and isinstance(event.get("message"), dict)
        and event["message"].get("role") == "assistant"
    ]
    if not ended:
        return None
    message = ended[-1]
    reason = message.get("stopReason")
    if reason != "stop":
        logger.warning("[omp] no answer class=omp_stop_%s", reason)
        return None
    parts = message.get("content") or []
    text = "".join(
        str(part.get("text", "")) for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )
    return text if text.strip() else None


OMP_POLICY_SHIM = Path(__file__).with_name("omp_policy.ts")
POLICY_TIMEOUT_MS = 10_000
AgentResult = tuple[list[dict[str, Any]], int, str, int | None, str | None, int, NativeCompletion]


async def run_omp_prompt(
    cfg_binary: str,
    paths: OpenCodePaths,
    prompt: str,
    *,
    model: str,
    system_prompt: str,
    timeout: int,
) -> str | None:
    """Run one tool-free omp call and return its answer, or None on any failure.

    Args:
        cfg_binary: Configured ``omp_binary`` (name or path); never exec'd raw.
        paths: Private omp roots; the subprocess HOME/XDG and cwd live here.
        prompt: Prompt piped on stdin; never logged.
        model: omp model selector.
        system_prompt: System prompt passed with ``--system-prompt``.
        timeout: Wall-clock bound in seconds, also passed as ``--max-time``.
    """
    binary = _resolve_binary(cfg_binary)
    if binary is None:
        logger.warning("[omp] binary unavailable class=omp_binary_missing")
        return None
    paths.ensure()
    try:
        events, returncode, _, _ = await _collect(
            omp_argv(binary, model, system_prompt, timeout),
            paths.environment(),
            paths.home,
            prompt,
            timeout,
        )
    except Exception as exc:
        logger.warning("[omp] prompt failed class=%s", type(exc).__name__)
        return None
    if returncode != 0:
        logger.warning("[omp] prompt failed class=omp_exit_%s", returncode)
        return None
    return _final_answer(events)


_OMP_TURN_DONE = frozenset({"stop", "length", "toolUse"})


def _omp_provider_status(event: dict[str, Any]) -> int | None:
    """Return omp's integer ``errorStatus`` for a failed assistant turn, or None.

    Only a real integer from 100 to 599 counts. ``errorMessage`` is never read.
    """
    if event.get("type") != "message_end":
        return None
    message = event.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    if message.get("stopReason") != "error":
        return None
    status = message.get("errorStatus")
    if type(status) is int and 100 <= status <= 599:
        return status
    return None


def _omp_provider_error(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Map omp's last failed assistant turn to a provider error event, or None.

    A later assistant turn that completes (stop, length, toolUse) clears the
    failure, so a recovered run is not failed.
    """
    status: int | None = None
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        stop_reason = message.get("stopReason")
        if stop_reason == "error":
            status = _omp_provider_status(event)
        elif stop_reason in _OMP_TURN_DONE:
            status = None
    if status is None:
        return None
    return {"type": "error", "error": {"type": "provider.omp", "status": status}}


async def _collect(
    argv: list[str],
    env: dict[str, str],
    cwd: Path,
    prompt: str,
    timeout: float,
    stop_on_provider_error: bool = False,
) -> tuple[list[dict[str, Any]], int | None, int, str]:
    """Run one omp process; return ``(events, returncode, stderr_bytes, stderr_sample)``.

    Any failure, timeout, or cancellation reaps the process group before it raises.
    """
    process = None
    stdout_task = None
    stderr_task = None
    stopped = False

    def stop_when(event: dict[str, Any]) -> bool:
        nonlocal stopped
        if _omp_provider_status(event) is not None:
            stopped = True
        return stopped

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cwd),
            start_new_session=(os.name == "posix"),
        )
        assert process.stdin is not None
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        assert process.stdout is not None and process.stderr is not None
        stop_when_provider = stop_when if stop_on_provider_error else None
        stdout_task = asyncio.create_task(_consume_jsonl(process.stdout, stop_when_provider))
        stderr_task = asyncio.create_task(_consume_stderr(process.stderr))

        async def _drain() -> None:
            await stdout_task
            if stopped:
                await _terminate_process(process)
            await process.wait()
            await stderr_task

        await asyncio.wait_for(_drain(), timeout=timeout)
        events, _ = stdout_task.result()
        stderr_bytes, stderr_sample = stderr_task.result()
        return events, process.returncode, stderr_bytes, stderr_sample
    except BaseException:
        # Cancellation must not orphan the start_new_session process group.
        if process is not None:
            await _terminate_process(process)
        raise
    finally:
        for task in (stdout_task, stderr_task):
            if task is not None and not task.done():
                task.cancel()


def _write_policy(directory: Path, policy: dict[str, Any]) -> Path:
    """Write the decider's policy file, readable only by this user."""
    path = directory / "policy.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(policy, handle)
    return path


def _agent_argv(
    binary: str,
    model: str,
    system_prompt: str,
    max_time: int,
    tools: tuple[str, ...],
    files: list[Path],
) -> list[str]:
    """Return the policy-gated argv: the shim is the only extension, tools are the mode allowlist."""
    return [
        binary, "-p", "--mode", "json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--max-time", str(max_time),
        "--no-session", "--no-extensions", "--no-skills",
        "--no-rules", "--no-context-files", "--no-title", "--no-lsp",
        "-e", str(OMP_POLICY_SHIM),
        "--tools", ",".join(tools),
        "--approval-mode", "write",
        *(f"@{path}" for path in files),
    ]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _failed_agent(error_class: str, started: float) -> AgentResult:
    return [], 0, "", None, error_class, _elapsed_ms(started), NativeCompletion(False, error_class)


async def run_omp_agent(
    binary: str,
    *,
    cwd: Path,
    roots: list[Path],
    mode: str,
    write_mode: str,
    write_dirs: list[str] | None,
    system_prompt: str,
    prompt: str,
    model: str,
    files: list[Path],
    timeout: float,
    stop_on_provider_error: bool = False,
) -> AgentResult:
    """Run one policy-gated omp reply and return the ``_execute_agent`` tuple shape.

    Each tool call passes ``omp_policy.ts``, which fails closed. The policy file
    is 0600 and lives in a temp dir removed on return.
    """
    started = time.monotonic()
    if not OMP_POLICY_SHIM.is_file():
        # Without the shim, --tools alone would leave read paths open.
        return _failed_agent("policy_unavailable", started)
    paths = resolve_opencode_paths(tool="omp")
    paths.ensure()
    max_time = max(1, math.ceil(timeout))
    with tempfile.TemporaryDirectory(prefix="oi-omp-policy-") as scratch:
        policy = _write_policy(
            Path(scratch),
            {
                "python": sys.executable,
                "timeout_ms": POLICY_TIMEOUT_MS,
                "mode": mode,
                "write_mode": write_mode,
                "roots": [str(root.resolve()) for root in roots],
                "cwd": str(cwd.resolve()),
                "write_dirs": write_dirs,
            },
        )
        env = paths.environment()
        env["OI_OMP_POLICY"] = str(policy)
        env["PI_EDIT_VARIANT"] = "replace"
        argv = _agent_argv(binary, model, system_prompt, max_time, mode_tools(mode), files)
        try:
            events, returncode, stderr_bytes, stderr_sample = await _collect(
                argv, env, cwd, prompt, timeout, stop_on_provider_error=stop_on_provider_error
            )
        except asyncio.TimeoutError:
            return _failed_agent("timeout", started)
        except _ProtocolError as exc:
            return _failed_agent(exc.error_class, started)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            logger.warning("[omp] process boundary failed: %s", type(exc).__name__)
            return _failed_agent("process_error", started)
    error_class: str | None = None
    if returncode != 0:
        error_class = "nonzero_exit"
    elif _final_answer(events) is None:
        error_class = "empty_response"
    provider_event = _omp_provider_error(events)
    if provider_event is not None:
        error_class = _provider_class(provider_event["error"]["status"]) or error_class
        events = [*events, provider_event]
    completion = NativeCompletion(error_class is None, error_class)
    return events, stderr_bytes, stderr_sample, returncode, error_class, _elapsed_ms(started), completion


def omp_select_text(
    events: list[dict[str, Any]],
    completion: NativeCompletion,
) -> tuple[str, str | None, str | None, int]:
    """Return ``(text, session_id, error, text_parts)``, the ``_select_final_text_v2`` shape.

    omp has no native session id, so the session slot is always None.
    """
    if not completion.completed:
        return "", None, completion.error_class, 0
    text = _final_answer(events) or ""
    return text, None, None, 1 if text else 0
