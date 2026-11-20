"""Tool-free and policy-gated omp paths: argv isolation, env scrub, success rule, reaping.

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
from oi_agent.opencode.omp import OMP_POLICY_SHIM, omp_argv, run_omp_agent
from oi_agent.opencode.omp_policy import mode_tools
from oi_agent.opencode.runner import OpenCodeRunner

REQUIRED_FLAGS = (
    "--no-tools", "--no-session", "--no-extensions", "--no-skills",
    "--no-rules", "--no-context-files", "--no-title", "--no-lsp",
)
FORBIDDEN_FLAGS = (
    "--tools", "-e", "--extension", "--hook", "--trusted-extension",
    "--approval-mode", "--yolo", "--continue", "--resume", "--config",
)

POLICY_REQUIRED_FLAGS = (
    "--no-session", "--no-extensions", "--no-skills", "--no-rules",
    "--no-context-files", "--no-title", "--no-lsp",
)
POLICY_FORBIDDEN_FLAGS = (
    "--no-tools", "--trusted-extension", "--continue", "--resume", "--session",
    "--yolo", "--auto-approve", "--config", "--extension", "--hook",
)

_ASSISTANT = {"type": "message_end", "message": {
    "role": "assistant", "stopReason": "stop",
    "content": [{"type": "text", "text": "ok"}],
}}
_RESEARCH = {"type": "message_end", "message": {
    "role": "assistant", "stopReason": "stop",
    "content": [{"type": "text", "text": '{"summary": "found it", "sources": ["https://example.com/a"]}'}],
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
    "research": [_RESEARCH],
}


def _omp_script(tmp_path, behavior="stop"):
    """Create a fake omp executable that records its inputs before answering."""
    trace = tmp_path / "omp-trace.json"
    exit_code = 1 if behavior == "exit_one" else 0
    body = f'''#!{sys.executable}
import json, os, sys, time
policy_path = os.environ.get("OI_OMP_POLICY")
policy = json.load(open(policy_path, encoding="utf-8")) if policy_path else None
with open({str(trace)!r}, "w", encoding="utf-8") as handle:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "pid": os.getpid(),
               "cwd": os.getcwd(), "env": dict(os.environ), "policy": policy}}, handle)
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
async def test_omp_model_overrides_every_raw_model(tmp_path, monkeypatch):
    """omp_model wins over opencode_model and per-call model overrides."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path)
    cfg = _config(binary, omp_model="anthropic/claude-haiku-5-5")
    assert await OpenCodeRunner(cfg).run_raw_prompt("x", timeout=5, model="zai/other") == "ok"
    argv = json.loads(trace_path.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--model") + 1] == "anthropic/claude-haiku-5-5"


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
async def test_omp_harness_classifier_stays_tool_free(tmp_path, monkeypatch):
    """Classifier calls run omp with the tool-free argv."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path)
    runner = OpenCodeRunner(_config(binary))
    assert await runner.classify_web_request("is this a question?") == "ok"
    argv = json.loads(trace_path.read_text(encoding="utf-8"))["argv"]
    assert "--no-tools" in argv


@pytest.mark.asyncio
async def test_omp_research_argv_web_tools_only(tmp_path, monkeypatch):
    """Web research under omp gets web_search and public reads, no repo root, and an empty cwd."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path, "research")
    runner = OpenCodeRunner(_config(binary))
    result = await runner.run_web_research("q", ["https://example.com/a"], max_sources=3)
    assert result.ok is True
    assert "https://example.com/a" in result.sources
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    argv = trace["argv"]
    assert argv[argv.index("--tools") + 1] == "read,web_search"
    assert argv[argv.index("--approval-mode") + 1] == "write"
    assert argv[argv.index("-e") + 1] == str(OMP_POLICY_SHIM)
    assert trace["policy"]["mode"] == "research"
    assert trace["policy"]["write_mode"] == "read-only"
    assert trace["policy"]["roots"] == []
    assert "oi-research-cwd-" in trace["cwd"]
    assert "https://example.com/a" in trace["stdin"]


@pytest.mark.asyncio
async def test_opencode_harness_never_runs_omp(tmp_path, monkeypatch):
    """With harness=opencode, raw prompts keep using OpenCode and omp stays unused."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    binary, trace_path = _omp_script(tmp_path)
    cfg = _config(binary, harness="opencode")
    assert await OpenCodeRunner(cfg).run_raw_prompt("x", timeout=5) is None
    assert not trace_path.exists()


def _flag_value(argv, flag):
    return argv[argv.index(flag) + 1]


@pytest.mark.asyncio
async def test_omp_policy_argv_is_gated_and_isolated(tmp_path, monkeypatch):
    """Tool-enabled omp loads only the policy shim with an explicit allowlist, and never a forbidden flag."""
    operator_home = tmp_path / "operator"
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("FOO_TOKEN", "secret-value")
    repo = tmp_path / "repo"
    repo.mkdir()
    binary, trace_path = _omp_script(tmp_path)
    _events, _sb, _ss, _rc, error_class, _dur, completion = await run_omp_agent(
        str(binary), cwd=repo, roots=[repo], mode="repo", write_mode="read-only",
        write_dirs=None, system_prompt="be terse", prompt="Do it",
        model="openai/gpt-5.2", files=[], timeout=5,
    )
    assert error_class is None and completion.completed

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    argv, env, policy = trace["argv"], trace["env"], trace["policy"]
    for flag in POLICY_REQUIRED_FLAGS:
        assert flag in argv
    for flag in POLICY_FORBIDDEN_FLAGS:
        assert flag not in argv
    assert _flag_value(argv, "-e") == str(OMP_POLICY_SHIM)
    assert _flag_value(argv, "--tools") == ",".join(mode_tools("repo"))
    assert _flag_value(argv, "--approval-mode") == "write"
    assert _flag_value(argv, "--model") == "openai/gpt-5.2"
    assert _flag_value(argv, "--system-prompt") == "be terse"
    assert trace["stdin"] == "Do it"

    root = operator_home.resolve() / ".local" / "share" / "oi" / "omp"
    assert Path(env["HOME"]) == root / "home"
    assert env["PI_EDIT_VARIANT"] == "replace"
    assert "FOO_TOKEN" not in env
    assert policy["mode"] == "repo"
    assert policy["write_mode"] == "read-only"
    assert [Path(p).resolve() for p in policy["roots"]] == [repo.resolve()]


@pytest.mark.asyncio
async def test_omp_policy_fails_closed_without_shim(tmp_path, monkeypatch):
    """A missing policy shim means omp is never spawned and the call fails closed."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr("oi_agent.opencode.omp.OMP_POLICY_SHIM", tmp_path / "missing.ts")
    repo = tmp_path / "repo"
    repo.mkdir()
    binary, trace_path = _omp_script(tmp_path)
    _events, _sb, _ss, _rc, error_class, _dur, _completion = await run_omp_agent(
        str(binary), cwd=repo, roots=[repo], mode="repo", write_mode="read-only",
        write_dirs=None, system_prompt="s", prompt="p",
        model="openai/gpt-5.2", files=[], timeout=5,
    )
    assert error_class == "policy_unavailable"
    assert not trace_path.exists()


@pytest.mark.asyncio
async def test_omp_policy_timeout_reaps_process(tmp_path, monkeypatch):
    """A policy-gated call past its wall-clock bound kills the omp process."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    repo = tmp_path / "repo"
    repo.mkdir()
    binary, trace_path = _omp_script(tmp_path, "sleep")
    _events, _sb, _ss, _rc, error_class, _dur, _completion = await run_omp_agent(
        str(binary), cwd=repo, roots=[repo], mode="repo", write_mode="read-only",
        write_dirs=None, system_prompt="s", prompt="p",
        model="openai/gpt-5.2", files=[], timeout=1,
    )
    assert error_class == "timeout"
    pid = json.loads(trace_path.read_text(encoding="utf-8"))["pid"]
    assert _pid_gone(pid)


@pytest.mark.asyncio
async def test_omp_external_runs_in_disposable_root_without_native_setup(tmp_path, monkeypatch):
    """External omp runs use an empty disposable cwd and skip OpenCode's native setup."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    repo = tmp_path / "repo"
    repo.mkdir()

    def _no_native(*_args, **_kwargs):
        raise AssertionError("omp runs must not initialize native OpenCode storage")

    monkeypatch.setattr("oi_agent.opencode.runner.initialize_native_schema", _no_native)
    binary, trace_path = _omp_script(tmp_path)
    runner = OpenCodeRunner(_config(binary))
    disposable = await runner._disposable_run_root(str(binary))
    assert disposable is not None
    base = disposable.base
    try:
        events, _sb, _ss, _rc, error_class, _dur, completion = await runner._execute_agent(
            binary=str(binary), root=repo, additional_roots=[], prompt_str="sys",
            prompt="Read it", disposable=disposable, omp_mode="external",
        )
        text, _sid, selection_error, _parts = runner._select_answer(events, completion)
    finally:
        disposable.close()
    assert error_class is None and selection_error is None and text == "ok"

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert Path(trace["cwd"]).resolve() == base.resolve()
    assert trace["policy"]["mode"] == "external"
    assert [Path(p).resolve() for p in trace["policy"]["roots"]] == [base.resolve()]
    assert _flag_value(trace["argv"], "--tools") == ",".join(mode_tools("external"))


@pytest.mark.asyncio
async def test_omp_review_scopes_head_and_base_read_only(tmp_path, monkeypatch):
    """Review runs under omp read only the verified head and base snapshots."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    head, base = tmp_path / "head", tmp_path / "base"
    head.mkdir()
    base.mkdir()
    binary, trace_path = _omp_script(tmp_path)
    runner = OpenCodeRunner(_config(binary))
    _events, _sb, _ss, _rc, error_class, _dur, _completion = await runner._execute_agent(
        binary=str(binary), root=head, additional_roots=[base], prompt_str="sys",
        prompt="Review", omp_mode="review",
    )
    assert error_class is None

    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    assert Path(trace["cwd"]).resolve() == head.resolve()
    assert trace["policy"]["mode"] == "review"
    assert trace["policy"]["write_mode"] == "read-only"
    assert [Path(p).resolve() for p in trace["policy"]["roots"]] == [head.resolve(), base.resolve()]
    assert _flag_value(trace["argv"], "--tools") == ",".join(mode_tools("review"))


def test_omp_harness_uses_one_attempt_on_omp_model(tmp_path):
    """omp runs one attempt on omp_model; OpenCode fallback models are never used for omp."""
    binary, _ = _omp_script(tmp_path)
    runner = OpenCodeRunner(_config(
        binary, omp_model="anthropic/claude-haiku-5-5",
        opencode_fallback_models=["openai/gpt-5.4"],
    ))
    assert runner._agent_models() == ["anthropic/claude-haiku-5-5"]
