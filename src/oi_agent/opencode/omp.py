"""Tool-free single-prompt calls through omp, isolated in a private home.

Only raw prompts (classifiers, summaries) may run here. Repository replies and
research stay on OpenCode. Every call runs with ``--no-tools`` and no session,
extensions, skills, rules, or context files, so the model has no authority.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from ..config import _resolve_binary
from .paths import OpenCodePaths
from .runner import _consume_jsonl, _consume_stderr, _terminate_process

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
    process = None
    stdout_task = None
    stderr_task = None
    try:
        process = await asyncio.create_subprocess_exec(
            *omp_argv(binary, model, system_prompt, timeout),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=paths.environment(),
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
        if process.returncode != 0:
            logger.warning("[omp] prompt failed class=omp_exit_%s", process.returncode)
            return None
        events, _ = stdout_task.result()
        return _final_answer(events)
    except asyncio.CancelledError:
        # Cancellation must not orphan the start_new_session process group.
        if process is not None:
            await _terminate_process(process)
        raise
    except Exception as exc:
        logger.warning("[omp] prompt failed class=%s", type(exc).__name__)
        if process is not None:
            await _terminate_process(process)
        return None
    finally:
        for task in (stdout_task, stderr_task):
            if task is not None and not task.done():
                task.cancel()
