"""Tests for the opt-in docs-only write mode (.md/.txt + folder gate)."""

import fnmatch
import shutil
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from oi_agent.config import Config, WatchTarget, load_config, save_config
from oi_agent.opencode.policy import (
    build_agent_config,
    build_permission_policy,
    normalize_write_dirs,
)
from oi_agent.opencode.prompt import build_prompt
from oi_agent.opencode.runner import _collect_doc_attachments


def _effective(policy: list[dict[str, str]], action: str, resource: str) -> str:
    """Apply native v2 last-matching-rule-wins semantics to one request."""
    outcome = "deny"
    for rule in policy:
        if rule["action"] not in (action, "*"):
            continue
        if fnmatch.fnmatchcase(resource, rule["resource"]) or rule["resource"] == "*":
            outcome = rule["effect"]
    return outcome


def _binary() -> str:
    """Return a portable executable accepted by config validation."""
    return shutil.which("true") or "/bin/true"


def _valid_config() -> Config:
    """Build a valid isolated test configuration."""
    cfg = Config(opencode_binary=_binary(), opencode_model="provider/model")
    cfg.watches = [WatchTarget(channel_id=111, repo_path="/srv/repo")]
    return cfg


def _git_repo(tmp_path: Path) -> Path:
    """Create a minimal committed git fixture."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    return repo


# --- policy ---------------------------------------------------------------


def test_docs_only_allows_md_txt_and_denies_code(tmp_path):
    """Docs-only grants .md/.txt edits; code, secrets, and other tools stay denied."""
    repo = _git_repo(tmp_path)
    policy = build_permission_policy(repo, write_mode="docs-only")
    assert policy[0] == {"action": "*", "resource": "*", "effect": "deny"}
    assert _effective(policy, "edit", "notes/plan.md") == "allow"
    assert _effective(policy, "edit", "notes/plan.txt") == "allow"
    assert _effective(policy, "edit", "src/app.py") == "deny"
    assert _effective(policy, "edit", ".env") == "deny"
    assert _effective(policy, "edit", "sub/.git/config") == "deny"
    assert _effective(policy, "shell", "*") == "deny"
    assert _effective(policy, "webfetch", "*") == "deny"
    assert _effective(policy, "subagent", "*") == "deny"
    # Repository reads stay granted under docs-only mode.
    assert _effective(policy, "read", "src/main.py") == "allow"


def test_docs_only_folder_gate_narrows_scope(tmp_path):
    """With write_dirs set, only inside that directory is granted.

    The boundary pattern is ``<scope>/*<ext>``. Because ``*`` spans ``/`` it
    covers nested files, but a sibling directory sharing the text prefix
    (``docs-private/`` when the scope is ``docs``) must never match: the
    parent's real native red proved the old ``<scope>*<ext>`` prefix grant
    created ``docs-private/scope-test.md``.
    """
    repo = _git_repo(tmp_path)
    policy = build_permission_policy(
        repo, write_mode="docs-only", write_dirs=["docs"]
    )
    assert _effective(policy, "edit", "docs/plan.md") == "allow"
    assert _effective(policy, "edit", "docs/deep/note.md") == "allow"
    assert _effective(policy, "edit", "docs/plan.txt") == "allow"
    assert _effective(policy, "edit", "docs-private/plan.md") == "deny"
    assert _effective(policy, "edit", "docs-private/scope-test.md") == "deny"
    assert _effective(policy, "edit", "root.md") == "deny"
    assert not any(
        rule["action"] == "edit" and rule["effect"] == "allow"
        and fnmatch.fnmatchcase("docs-private/plan.md", rule["resource"])
        for rule in policy
    )


def test_docs_only_without_provable_location_boundary_denies_all(tmp_path):
    """A Location that is not a worktree root grants no edits, scoped or not."""
    for kwargs in ({}, {"write_dirs": ["docs"]}):
        policy = build_permission_policy(tmp_path, write_mode="docs-only", **kwargs)
        assert _effective(policy, "edit", "plan.md") == "deny"
        assert _effective(policy, "edit", "docs/plan.md") == "deny"
        assert _effective(policy, "edit", "docs/../escape.md") == "deny"


def test_unknown_write_mode_fails_closed_to_read_only(tmp_path):
    """Anything but docs-only keeps every edit denied."""
    policy = build_permission_policy(tmp_path, write_mode="bogus")
    assert _effective(policy, "edit", "plan.md") == "deny"
    assert not any(
        rule["action"] == "edit" and rule["effect"] == "allow" for rule in policy
    )


def test_normalize_write_dirs_never_widens():
    """Valid scopes are cleaned; an all-invalid scope refuses to widen."""
    assert normalize_write_dirs(["docs/plans/", "docs/memory"]) == ["docs/plans", "docs/memory"]
    assert normalize_write_dirs(["docs"]) == ["docs"]
    assert normalize_write_dirs(None) is None
    assert normalize_write_dirs([]) is None
    with pytest.raises(ValueError, match="refusing to widen"):
        normalize_write_dirs(["../evil", "/abs", ".", "", None])  # type: ignore[list-item]


def test_agent_config_carries_docs_only_description(tmp_path):
    """Agent description reflects the granted mode, on the v2 agents map."""
    repo = _git_repo(tmp_path)
    prompt = build_prompt("voice")
    ro = build_agent_config("p/m", 10, repo, prompt)["agents"]["oi"]
    docs = build_agent_config("p/m", 10, repo, prompt, write_mode="docs-only")["agents"]["oi"]
    assert "Read-only" in ro["description"]
    assert "docs-only" in docs["description"]
    assert {"action": "edit", "resource": "*.md", "effect": "allow"} in docs["permissions"]
    assert not any(
        rule["action"] == "edit" and rule["effect"] == "allow"
        for rule in ro["permissions"]
    )


# --- prompt ---------------------------------------------------------------


def test_prompt_read_only_keeps_create_ban():
    """Default prompt still forbids all file creation."""
    prompt = build_prompt("voice")
    assert "Never edit, delete, create" in prompt
    assert "ONLY *.md and *.txt" not in prompt


def test_prompt_read_only_names_the_enable_command():
    """Read-only refusal points at the exact opt-in command."""
    prompt = build_prompt("voice")
    assert "disabled by configuration" in prompt
    assert "oi config set write_mode docs-only" in prompt


def test_prompt_docs_only_allows_docs_with_scope():
    """Docs-only prompt grants .md/.txt and names the folder gate."""
    prompt = build_prompt("voice", write_mode="docs-only", write_dirs=["docs/plans"])
    assert "ONLY *.md and *.txt" in prompt
    assert "docs/plans" in prompt
    assert "no code, no configs" in prompt


def test_prompt_docs_only_forbids_false_inability_claims():
    """Docs-only prompt bans 'no write tool / read-only' confabulation."""
    prompt = build_prompt("voice", write_mode="docs-only")
    assert "never claim you have no write tool" in prompt
    assert "WRITE THE FILE with the tools" in prompt
    assert "Never tell the user to create, paste, or commit the file" in prompt
    assert "never claim you have no write tool" not in build_prompt("voice")


# --- config + CLI ---------------------------------------------------------


def test_write_mode_roundtrip_and_validation(tmp_path):
    """write_mode/write_dirs survive save/load; bad values rejected."""
    cfg = _valid_config()
    cfg.write_mode = "docs-only"
    cfg.write_dirs = ["docs/plans", "docs/memory"]
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.write_mode == "docs-only"
    assert back.write_dirs == ["docs/plans", "docs/memory"]

    bad = _valid_config()
    bad.write_mode = "full-write"
    with pytest.raises(ValueError, match="write_mode"):
        save_config(bad, tmp_path / "bad.toml")

    evil = _valid_config()
    evil.write_dirs = ["../escape"]
    with pytest.raises(ValueError, match="write_dirs"):
        save_config(evil, tmp_path / "evil.toml")


def test_config_set_write_mode_and_dirs_cli_roundtrip(tmp_path):
    """oi config set accepts write_mode and comma-separated write_dirs."""
    from oi_agent.cli import app

    runner = CliRunner()
    path = tmp_path / "config.toml"
    save_config(_valid_config(), path)

    result = runner.invoke(app, ["config", "set", "write_mode", "docs-only", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert load_config(path).write_mode == "docs-only"

    result = runner.invoke(
        app, ["config", "set", "write_dirs", "docs/plans,docs/memory", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(path).write_dirs == ["docs/plans", "docs/memory"]

    shown = runner.invoke(app, ["config", "show", "--config", str(path)])
    assert shown.exit_code == 0, shown.output
    assert "write_mode = docs-only" in shown.output

    bad = runner.invoke(app, ["config", "set", "write_mode", "yolo", "--config", str(path)])
    assert bad.exit_code == 1
    assert load_config(path).write_mode == "docs-only"


# --- runner attachment collection -----------------------------------------


def test_collects_fresh_md_but_ignores_code_and_stale(tmp_path):
    """Only freshly touched .md/.txt qualify; code and old files ignored."""
    repo = _git_repo(tmp_path)
    since = time.time()
    fresh = repo / "plan.md"
    fresh.write_text("# plan\n", encoding="utf-8")
    (repo / "notes.txt").write_text("hi\n", encoding="utf-8")
    (repo / "evil.py").write_text("print(1)\n", encoding="utf-8")
    stale = repo / "old.md"
    stale.write_text("stale\n", encoding="utf-8")
    old_mtime = since - 3600
    import os

    os.utime(stale, (old_mtime, old_mtime))

    found = _collect_doc_attachments([repo], write_dirs=[], since_epoch=since)
    names = [Path(p).name for p in found]
    assert "plan.md" in names
    assert "notes.txt" in names
    assert "evil.py" not in names
    assert "old.md" not in names


def test_collect_respects_folder_gate(tmp_path):
    """Scoped mode only picks docs inside the allowed directories."""
    repo = _git_repo(tmp_path)
    since = time.time()
    scoped_dir = repo / "docs" / "plans"
    scoped_dir.mkdir(parents=True)
    (scoped_dir / "plan.md").write_text("# scoped\n", encoding="utf-8")
    (repo / "root.md").write_text("# root\n", encoding="utf-8")

    found = _collect_doc_attachments([repo], write_dirs=["docs/plans"], since_epoch=since)
    names = [Path(p).name for p in found]
    assert "plan.md" in names
    assert "root.md" not in names


# --- poster file upload ----------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_file_uploads_md_and_rejects_code(tmp_path, monkeypatch):
    """deliver_file POSTs multipart for .md and refuses other types."""
    from oi_agent.poster import Poster
    from oi_agent.store import Store

    cfg = Config()
    cfg.watches = [WatchTarget(channel_id=111, repo_path="/repo", post_hourly_cap=1)]
    store = Store(tmp_path / "state.db")
    captured: dict = {}
    doc = tmp_path / "plan.md"
    doc.write_text("# plan\n", encoding="utf-8")

    class FakeClient:
        """Async HTTP client recording multipart upload arguments."""

        def __init__(self, *args, **kwargs):
            """Accept the httpx client constructor arguments."""

        async def __aenter__(self):
            """Enter fake client."""
            return self

        async def __aexit__(self, *args):
            """Exit fake client."""
            return False

        async def post(self, url, headers, data, files):
            """Capture multipart payload and return created message ID."""
            captured["url"] = url
            captured["data"] = data
            captured["files"] = files
            return SimpleNamespace(status_code=200, text="", json=lambda: {"id": "4242"})

    monkeypatch.setattr("oi_agent.poster.httpx.AsyncClient", FakeClient)
    poster = Poster(cfg, store, "token")

    result = await poster.deliver_file(111, 111, str(doc), reply_to_message_id=777)
    assert result.ok is True
    assert result.discord_message_id == 4242
    assert captured["url"] == "https://discord.com/api/v10/channels/111/messages"
    assert "payload_json" in captured["data"]
    assert "files[0]" in captured["files"]
    # File upload is best-effort: it must not consume the hourly text cap.
    assert store.posts_in_last_hour(111) == 0

    bad = tmp_path / "evil.py"
    bad.write_text("print(1)\n", encoding="utf-8")
    denied = await poster.deliver_file(111, 111, str(bad))
    assert denied.ok is False
    assert denied.error_class == "file_type_denied"
