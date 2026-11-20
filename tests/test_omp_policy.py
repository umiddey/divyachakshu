"""P6a: fail-closed decider and TypeScript gate for omp tool calls.

Decider tests call ``decide`` directly. Shim tests run ``omp_policy.ts`` under
Bun with a fake ``tool_call`` registrar. Every test is offline.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from oi_agent.opencode import omp_policy
from oi_agent.opencode.omp_policy import decide, load_policy, main

SHIM = Path(omp_policy.__file__).with_suffix(".ts")
LOOPBACK = ".".join(["127", "0", "0", "1"])
FAIL_VERDICT = {"block": True, "reason": "blocked by oi policy: check failed"}
DRIVER = """
(async () => {
  const mod = await import(process.env.OI_TEST_SHIM);
  let handler;
  mod.default({ on: (name, fn) => { if (name === "tool_call") handler = fn; } });
  const verdict = handler({
    toolName: process.env.OI_TEST_TOOL,
    input: JSON.parse(process.env.OI_TEST_INPUT),
  });
  console.log(JSON.stringify(verdict ?? null));
})();
"""

READ_ALLOWED = [
    ("read", {"path": "README.md"}),
    ("read", {"path": "docs/a.md:1-5"}),
    ("read", {"path": "src/app.py:raw"}),
    ("read", {"path": "README.md;docs/a.md"}),
    ("grep", {"pattern": "x", "path": "src"}),
    ("grep", {"pattern": "x", "path": "README.md"}),
    ("glob", {"pattern": "*.md", "path": "docs"}),
]
READ_DENIED = [
    ("read", {"path": "../outside.txt"}),
    ("read", {"path": "link.md"}),
    ("read", {"path": "README.md;../outside.txt"}),
    ("read", {"path": ".env"}),
    ("read", {"path": "secrets.md"}),
    ("read", {"path": "docs/token.md"}),
    ("read", {"path": "docs/missing.md"}),
    ("read", {"path": "README.md?q=x"}),
    ("read", {"path": "~/.omp/agent/config.yml"}),
    ("read", {"path": "archive.zip:member"}),
    ("read", {"path": "docs/a.md\nREADME.md"}),
    ("read", {"path": "https://example.com/"}),
    ("grep", {"pattern": "x", "path": "/etc"}),
    ("grep", {"pattern": "x", "path": "../"}),
    ("glob", {"pattern": "../*.txt", "path": "."}),
    ("grep", {"pattern": "x"}),
    ("grep", {"pattern": "x", "path": "docs"}),
    ("grep", {"pattern": "x", "path": "src", "glob": "*.env"}),
    ("grep", {"pattern": "x", "path": "src", "include": "*token*"}),
    ("grep", {"pattern": "x", "path": "src/*.py"}),
    ("glob", {"pattern": "*secret*", "path": "."}),
    ("glob", {"pattern": "*.md", "path": "docs", "glob": "*.pem"}),
    ("glob", {"pattern": "/etc/*", "path": "."}),
]
DENIED_TOOLS = ["bash", "write", "task", "web_search", "mcp__brightdata_web_search_engine"]
EDIT_ALLOWED = [
    {"path": "docs/a.md"},
    {"path": "README.md"},
    {"paths": ["docs/a.md", "README.md"]},
]
EDIT_DENIED = [
    {"path": "src/app.py"},
    {"path": "docs/a.md", "paths": ["src/app.py"]},
    {"path": "docs/new.md"},
    {"path": "docs/*.md"},
    {"path": "docs/a.md:1-2"},
    {"path": "link.md"},
    {"path": "../outside.md"},
    {"path": ".env"},
    {"path": "docs/token.md"},
    {"path": 7},
    {},
]
PUBLIC_URLS = ["https://example.com/page", "http://example.org/a?b=c", "https://docs.python.org/3/"]
NON_PUBLIC_URLS = [
    f"http://{LOOPBACK}/",
    "http://[::1]/",
    "http://10.0.0.5/",
    "https://intranet.local/",
    "https://service.internal/",
    "file:///etc/passwd",
    "ftp://example.com/",
    "https://user@example.com/",
    "https://example.com/a b",
    "https://example.com:0/",
    "https://example.com;x",
    "https://localhost/",
]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "src").mkdir()
    (root / "README.md").write_text("# readme\n", encoding="utf-8")
    (root / "docs" / "a.md").write_text("doc\n", encoding="utf-8")
    (root / "docs" / "token.md").write_text("tok\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".env").write_text("KEY=1\n", encoding="utf-8")
    (root / "secrets.md").write_text("s\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("out\n", encoding="utf-8")
    (root / "link.md").symlink_to(outside)
    return root


def _policy_file(tmp_path: Path, repo: Path, **overrides: object) -> Path:
    body: dict[str, object] = {
        "mode": "repo",
        "write_mode": "docs-only",
        "roots": [str(repo)],
        "cwd": str(repo),
        "write_dirs": None,
        "python": sys.executable,
        "timeout_ms": 30_000,
    }
    body.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    path.chmod(0o600)
    return path


def _load(tmp_path: Path, repo: Path, **overrides: object):
    return load_policy(_policy_file(tmp_path, repo, **overrides))


@pytest.mark.parametrize(("tool", "tool_input"), READ_ALLOWED)
def test_repo_read_like_allowed_inside_root(tmp_path: Path, repo: Path, tool: str, tool_input: dict) -> None:
    assert decide(_load(tmp_path, repo), tool, tool_input) == (True, "")


@pytest.mark.parametrize(("tool", "tool_input"), READ_DENIED)
def test_repo_read_like_denied_outside_or_secret(tmp_path: Path, repo: Path, tool: str, tool_input: dict) -> None:
    allowed, reason = decide(_load(tmp_path, repo), tool, tool_input)
    assert not allowed
    assert reason


@pytest.mark.parametrize("tool", DENIED_TOOLS)
def test_repo_denies_tools_outside_the_mode_allowlist(tmp_path: Path, repo: Path, tool: str) -> None:
    allowed, reason = decide(_load(tmp_path, repo), tool, {"query": "x", "path": "README.md"})
    assert not allowed
    assert "not allowed" in reason


@pytest.mark.parametrize("tool_input", EDIT_ALLOWED)
def test_edit_allowed_for_docs_in_repo_docs_only(tmp_path: Path, repo: Path, tool_input: dict) -> None:
    assert decide(_load(tmp_path, repo), "edit", tool_input) == (True, "")


@pytest.mark.parametrize("tool_input", EDIT_DENIED)
def test_edit_denied_for_code_secrets_globs_and_selectors(tmp_path: Path, repo: Path, tool_input: dict) -> None:
    allowed, _ = decide(_load(tmp_path, repo), "edit", tool_input)
    assert not allowed


def test_edit_denied_in_read_only_write_mode(tmp_path: Path, repo: Path) -> None:
    policy = _load(tmp_path, repo, write_mode="read-only")
    assert decide(policy, "edit", {"path": "docs/a.md"}) == (False, "edit is not allowed in this mode")


def test_edit_scoped_to_write_dirs(tmp_path: Path, repo: Path) -> None:
    policy = _load(tmp_path, repo, write_dirs=["docs"])
    assert decide(policy, "edit", {"path": "docs/a.md"}) == (True, "")
    assert decide(policy, "edit", {"path": "README.md"})[0] is False


@pytest.mark.parametrize("mode", ["review", "external", "research"])
def test_edit_denied_outside_repo_mode(tmp_path: Path, repo: Path, mode: str) -> None:
    assert decide(_load(tmp_path, repo, mode=mode), "edit", {"path": "docs/a.md"})[0] is False


@pytest.mark.parametrize("mode", ["review", "external"])
def test_review_and_external_read_inside_root_but_never_search_web(tmp_path: Path, repo: Path, mode: str) -> None:
    policy = _load(tmp_path, repo, mode=mode)
    assert decide(policy, "read", {"path": "README.md"}) == (True, "")
    assert decide(policy, "web_search", {"query": "x"})[0] is False


def test_research_allows_web_search_and_public_http_reads(tmp_path: Path, repo: Path) -> None:
    policy = _load(tmp_path, repo, mode="research")
    assert decide(policy, "web_search", {"query": "omp harness"}) == (True, "")
    for url in PUBLIC_URLS:
        assert decide(policy, "read", {"path": url}) == (True, ""), url


@pytest.mark.parametrize("url", NON_PUBLIC_URLS)
def test_research_denies_non_public_or_malformed_urls(tmp_path: Path, repo: Path, url: str) -> None:
    assert decide(_load(tmp_path, repo, mode="research"), "read", {"path": url})[0] is False


@pytest.mark.parametrize(("tool", "tool_input"), [
    ("grep", {"pattern": "x", "path": "."}),
    ("glob", {"pattern": "*", "path": "."}),
    ("edit", {"path": "docs/a.md"}),
    ("bash", {"command": "id"}),
])
def test_research_denies_local_and_mutating_tools(tmp_path: Path, repo: Path, tool: str, tool_input: dict) -> None:
    assert decide(_load(tmp_path, repo, mode="research"), tool, tool_input)[0] is False


def test_load_policy_rejects_bad_fields(tmp_path: Path, repo: Path) -> None:
    with pytest.raises(ValueError):
        load_policy(_policy_file(tmp_path, repo, mode="bogus"))
    with pytest.raises(ValueError):
        load_policy(_policy_file(tmp_path, repo, write_mode="writable"))
    with pytest.raises(ValueError):
        load_policy(_policy_file(tmp_path, repo, roots=["relative/dir"]))
    with pytest.raises(ValueError):
        load_policy(_policy_file(tmp_path, repo, cwd=str(tmp_path)))


async def _run_driver(bun: str, env: dict[str, str]) -> tuple[int | None, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        bun, "-e", DRIVER,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except TimeoutError:
        proc.kill()
        raise
    return proc.returncode, stdout, stderr


def _verdict(policy_path: Path, tool: str, tool_input: dict) -> object:
    """Run the real TypeScript gate under Bun and return its verdict (None = allowed)."""
    bun = shutil.which("bun")
    if bun is None:
        pytest.fail("bun is required to run the omp_policy.ts gate tests")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "OI_OMP_POLICY": str(policy_path),
        "OI_TEST_SHIM": str(SHIM),
        "OI_TEST_TOOL": tool,
        "OI_TEST_INPUT": json.dumps(tool_input),
    }
    returncode, stdout, stderr = asyncio.run(_run_driver(bun, env))
    assert returncode == 0, stderr.decode()
    return json.loads(stdout)


def _fake_python(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-python"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_shim_allows_in_root_read(tmp_path: Path, repo: Path) -> None:
    assert _verdict(_policy_file(tmp_path, repo), "read", {"path": "README.md"}) is None


def test_shim_blocks_secret_read_with_decider_reason(tmp_path: Path, repo: Path) -> None:
    verdict = _verdict(_policy_file(tmp_path, repo), "read", {"path": ".env"})
    assert isinstance(verdict, dict)
    assert verdict["block"] is True
    assert verdict["reason"]


def test_shim_fails_closed_when_policy_file_is_missing(tmp_path: Path) -> None:
    assert _verdict(tmp_path / "missing.json", "read", {"path": "README.md"}) == FAIL_VERDICT


def test_shim_fails_closed_on_relative_python(tmp_path: Path, repo: Path) -> None:
    policy = _policy_file(tmp_path, repo, python="python3")
    assert _verdict(policy, "read", {"path": "README.md"}) == FAIL_VERDICT


@pytest.mark.parametrize(("body", "timeout_ms"), [
    ("exit 3", 30_000),
    ("echo not-json", 30_000),
    ("exec sleep 30", 300),
])
def test_shim_fails_closed_on_decider_error_bad_output_or_timeout(
    tmp_path: Path, repo: Path, body: str, timeout_ms: int,
) -> None:
    fake = _fake_python(tmp_path, body)
    policy = _policy_file(tmp_path, repo, python=str(fake), timeout_ms=timeout_ms)
    assert _verdict(policy, "read", {"path": "README.md"}) == FAIL_VERDICT


def _cli(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, policy: Path | None, event: str):
    if policy is None:
        monkeypatch.delenv("OI_OMP_POLICY", raising=False)
    else:
        monkeypatch.setenv("OI_OMP_POLICY", str(policy))
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    assert main() == 0
    return json.loads(capsys.readouterr().out)


def test_cli_allows_and_blocks_by_verdict(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = _policy_file(tmp_path, repo)
    allowed = _cli(capsys, monkeypatch, policy, json.dumps({"toolName": "read", "input": {"path": "README.md"}}))
    assert allowed == {"block": False, "reason": ""}
    blocked = _cli(capsys, monkeypatch, policy, json.dumps({"toolName": "read", "input": {"path": ".env"}}))
    assert blocked["block"] is True
    assert blocked["reason"]


@pytest.mark.parametrize("event", [
    "not json",
    json.dumps({"toolName": "read", "input": "README.md"}),
    json.dumps({"toolName": 7, "input": {"path": "README.md"}}),
])
def test_cli_blocks_malformed_events(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, event: str,
) -> None:
    verdict = _cli(capsys, monkeypatch, _policy_file(tmp_path, repo), event)
    assert verdict == {"block": True, "reason": "policy check failed"}


def test_cli_blocks_when_policy_env_is_unset(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = json.dumps({"toolName": "read", "input": {"path": "README.md"}})
    assert _cli(capsys, monkeypatch, None, event) == {"block": True, "reason": "policy check failed"}
