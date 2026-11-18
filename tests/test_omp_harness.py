"""Tool-free omp prompt path: argv isolation, env scrub, success rule, and reaping.

Every test uses a fake omp executable; no real model is ever called.
"""

import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from oi_agent.config import Config
from oi_agent.opencode.omp import omp_argv
from oi_agent.opencode.runner import OpenCodeRunner

REQUIRED_FLAGS = (
    "--no-tools", "--no-session", "--no-extensions", "--no-skills",
    "--no-rules", "--no-context-files", "--no-title", "--no-lsp",
)
FORBIDDEN_FLAGS = (
    "--tools", "-e", "--extension", "--hook", "--trusted-extension",
    "--approval-mode", "--yolo", "--continue", "--resume", "--config",
)

_ASSISTANT = {"type": "message_end", "message": {
    "role": "assistant", "stopReason": "stop",
    "content": [{"type": "text", "text": "ok"}],
}}
_EVENTS = {
    "stop": [_ASSISTANT],
    "aborted": [{"type": "message_end", "message": {
        "role": "assistant", "stopReason": "aborted", "content": [],
    }}],
    "blank": [{"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": "   "}],
    }}],
    "exit_one": [_ASSISTANT],
}


def _omp_script(tmp_path, behavior="stop"):
    """Create a fake omp executable that records its inputs before answering."""
    trace = tmp_path / "omp-trace.json"
    exit_code = 1 if behavior == "exit_one" else 0
    body = f'''#!{sys.executable}
import json, os, sys, time
with open({str(trace)!r}, "w", encoding="utf-8") as handle:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "pid": os.getpid(),
               "cwd": os.getcwd(), "env": dict(os.environ)}}, handle)
if {behavior!r} == "sleep":
    time.sleep(30)
for event in {_EVENTS.get(behavior, [])!r}:
    print(json.dumps(event))
sys.exit({exit_code})
'''
    path = tmp_path / f"fake-omp-{behavior}"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, trace


def _config(binary, **overrides):
    """Config that routes raw prompts to the fake omp binary."""
    values = {
        "harness": "omp",
        "omp_binary": str(binary),
        "opencode_binary": "/nonexistent/opencode",
        "opencode_model": "openai/gpt-5.2",
        "opencode_timeout_seconds": 5,
    }
    values.update(overrides)
    return Config(**values)


def _pid_gone(pid):
    return not os.path.exists(f"/proc/{pid}")


def test_omp_argv_is_tool_free_and_isolated():
    """The argv carries every isolation flag and none of the forbidden ones."""
    argv = omp_argv("/bin/omp", "openai/gpt-5.2", "be terse", 60)
    for flag in REQUIRED_FLAGS:
        assert flag in argv
    for flag in FORBIDDEN_FLAGS:
        assert flag not in argv
    assert argv[argv.index("--model") + 1] == "openai/gpt-5.2"
    assert argv[argv.index("--system-prompt") + 1] == "be terse"
    assert argv[argv.index("--max-time") + 1] == "60"


@pytest.mark.asyncio
async def test_omp_subprocess_is_isolated_and_receives_prompt(tmp_path, monkeypatch):
    """HOME/XDG point at the private omp roots, cwd is the private home, secrets stay out."""
    operator_home = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("FOO_TOKEN", "secret-value")
    monkeypatch.setenv("OI_X", "leak")
    binary, trace_path = _omp_script(tmp_path)
    res = await OpenCodeRunner(_config(binary)).run_raw_prompt(
        "Isolation probe", system_prompt="be terse", timeout=5,
    )
    assert res == "ok"

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    root = operator_home.resolve() / ".local" / "share" / "oi" / "omp"
    env = trace["env"]
    assert Path(env["HOME"]) == root / "home"
    assert Path(env["XDG_CONFIG_HOME"]) == root / "config"
    assert Path(env["XDG_DATA_HOME"]) == root / "data"
    assert Path(trace["cwd"]).resolve() == (root / "home").resolve()
    assert "FOO_TOKEN" not in env
    assert "OI_X" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    assert trace["stdin"] == "Isolation probe"
    assert trace["argv"][trace["argv"].index("--model") + 1] == "openai/gpt-5.2"
    assert trace["argv"][trace["argv"].index("--system-prompt") + 1] == "be terse"
    for flag in REQUIRED_FLAGS:
        assert flag in trace["argv"]


@pytest.mark.asyncio
async def test_omp_answer_requires_clean_stop_and_text(tmp_path, monkeypatch):
    """Exit 1, aborted stops, and blank text all fail closed to None."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    for behavior in ("exit_one", "aborted", "blank"):
        binary, _ = _omp_script(tmp_path, behavior)
        res = await OpenCodeRunner(_config(binary)).run_raw_prompt("x", timeout=5)
        assert res is None, behavior


@pytest.mark.asyncio
async def test_missing_omp_binary_fails_closed(tmp_path, monkeypatch):
    """An omp binary that cannot be resolved returns None without spawning anything."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    cfg = _config(tmp_path / "does-not-exist")
    assert await OpenCodeRunner(cfg).run_raw_prompt("x", timeout=5) is None


@pytest.mark.asyncio
async def test_omp_timeout_reaps_process(tmp_path, monkeypatch):
    """A call past its wall-clock bound returns None and kills the omp process."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path, "sleep")
    res = await OpenCodeRunner(_config(binary)).run_raw_prompt("x", timeout=1)
    assert res is None
    pid = json.loads(trace_path.read_text(encoding="utf-8"))["pid"]
    assert _pid_gone(pid)


@pytest.mark.asyncio
async def test_omp_cancel_reaps_process(tmp_path, monkeypatch):
    """Cancelling the caller kills the omp process group and re-raises cancellation."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path, "sleep")
    task = asyncio.create_task(
        OpenCodeRunner(_config(binary)).run_raw_prompt("x", timeout=30)
    )
    for _ in range(200):
        if trace_path.exists():
            try:
                pid = json.loads(trace_path.read_text(encoding="utf-8"))["pid"]
                break
            except json.JSONDecodeError:
                pass
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _pid_gone(pid)


@pytest.mark.asyncio
async def test_omp_harness_routes_tool_free_callers_only(tmp_path, monkeypatch):
    """Classifier calls use omp; web research never reaches omp."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, _ = _omp_script(tmp_path)
    runner = OpenCodeRunner(_config(binary))
    assert await runner.classify_web_request("is this a question?") == "ok"

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("omp must not run for web research")

    monkeypatch.setattr("oi_agent.opencode.omp.run_omp_prompt", _forbidden)
    result = await runner.run_web_research("q", [], max_sources=1)
    assert result.ok is False
    assert result.category == "binary_unavailable"


@pytest.mark.asyncio
async def test_opencode_harness_never_runs_omp(tmp_path, monkeypatch):
    """With harness=opencode, raw prompts keep using OpenCode and omp stays unused."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path)
    cfg = _config(binary, harness="opencode")
    assert await OpenCodeRunner(cfg).run_raw_prompt("x", timeout=5) is None
    assert not trace_path.exists()
