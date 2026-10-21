"""Behavioral tests for read-scoped pull provenance (real git, throwaway repos)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from oi_agent.opencode.provenance import git_pull, staleness_note


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _clone_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return (remote, seed, clone): bare remote, its author repo, and a clone."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(
        ["git", "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _git(seed, "config", "user.name", "T")
    _git(seed, "config", "user.email", "t@example.com")
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", str(remote), "main")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    _git(clone, "config", "user.name", "T")
    _git(clone, "config", "user.email", "t@example.com")
    return remote, seed, clone


def _upstream_commit_note(seed: Path, remote: Path, rel: str, body: str) -> None:
    path = seed / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    _git(seed, "add", rel)
    _git(seed, "commit", "-qm", f"add {rel}")
    _git(seed, "push", "-q", str(remote), "main")


def test_success_path_unchanged(tmp_path):
    _remote, _seed, clone = _clone_pair(tmp_path)
    result = git_pull(str(clone))
    assert result.ok is True
    assert result.cause == ""
    assert result.behind == 0
    assert result.upstream == "origin/main"
    assert result.last_pull is not None
    assert staleness_note("any", result) == ""


def test_failure_keeps_last_successful_pull_per_repo(tmp_path):
    _remote, _seed, clone = _clone_pair(tmp_path)
    first = git_pull(str(clone))
    assert first.ok is True
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    second = git_pull(str(clone))
    assert second.ok is False
    assert second.cause == "auth_or_network"
    assert second.last_pull == first.last_pull
    assert second.behind == 0


def test_differing_untracked_blocker_reported_and_untouched(tmp_path):
    _remote, seed, clone = _clone_pair(tmp_path)
    blocker = clone / "docs" / "plans" / "note.md"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("local draft\n", encoding="utf-8")
    _upstream_commit_note(seed, _remote, "docs/plans/note.md", "upstream version\n")
    result = git_pull(str(clone))
    assert result.ok is False
    assert result.cause == "untracked_would_be_overwritten"
    assert result.blocking_paths == ("docs/plans/note.md",)
    assert result.behind == 1
    assert blocker.read_text(encoding="utf-8") == "local draft\n"
    assert "docs/plans/note.md" not in _git(clone, "ls-files")


def test_staleness_note_carries_cause_behind_and_last_update(tmp_path):
    _remote, seed, clone = _clone_pair(tmp_path)
    blocker = clone / "docs" / "plans" / "note.md"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("local draft\n", encoding="utf-8")
    _upstream_commit_note(seed, _remote, "docs/plans/note.md", "upstream version\n")
    note = staleness_note("portal", git_pull(str(clone)))
    assert "reading an old copy of portal" in note
    assert "1 commits behind origin/main" in note
    assert "not since OI started" in note
    assert "cause: untracked_would_be_overwritten" in note


def test_diverged_branch_reported_with_behind_count(tmp_path):
    _remote, seed, clone = _clone_pair(tmp_path)
    (seed / "README.md").write_text("up\n", encoding="utf-8")
    _git(seed, "commit", "-aqm", "up")
    _git(seed, "push", "-q", str(_remote), "main")
    (clone / "README.md").write_text("local\n", encoding="utf-8")
    _git(clone, "commit", "-aqm", "local")
    result = git_pull(str(clone))
    assert result.ok is False
    assert result.cause == "diverged"
    assert result.behind == 1
    assert (clone / "README.md").read_text(encoding="utf-8") == "local\n"


def test_missing_upstream_reported(tmp_path):
    _remote, _seed, clone = _clone_pair(tmp_path)
    _git(clone, "branch", "--unset-upstream")
    result = git_pull(str(clone))
    assert result.ok is False
    assert result.cause == "no_upstream"
    assert result.upstream == ""
    assert result.behind == 0


def test_identical_untracked_blocker_heals(tmp_path):
    remote, seed, clone = _clone_pair(tmp_path)
    blocker = clone / "docs" / "plans" / "note.md"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("identical bytes\n", encoding="utf-8")
    _upstream_commit_note(seed, remote, "docs/plans/note.md", "identical bytes\n")
    result = git_pull(str(clone))
    assert result.ok is True
    assert result.cause == ""
    assert result.behind == 0
    assert "docs/plans/note.md" in _git(clone, "ls-files")
    assert blocker.read_text(encoding="utf-8") == "identical bytes\n"
    assert result.last_pull is not None


def test_mixed_blockers_change_nothing_on_disk(tmp_path):
    remote, seed, clone = _clone_pair(tmp_path)
    (clone / "docs").mkdir()
    (clone / "docs" / "same.md").write_text("same\n", encoding="utf-8")
    (clone / "docs" / "diff.md").write_text("local\n", encoding="utf-8")
    _upstream_commit_note(seed, remote, "docs/same.md", "same\n")
    _upstream_commit_note(seed, remote, "docs/diff.md", "upstream\n")
    result = git_pull(str(clone))
    assert result.ok is False
    assert result.cause == "untracked_would_be_overwritten"
    assert set(result.blocking_paths) == {"docs/same.md", "docs/diff.md"}
    assert (clone / "docs" / "same.md").read_text(encoding="utf-8") == "same\n"
    assert "docs/same.md" not in _git(clone, "ls-files")
    assert "docs/diff.md" not in _git(clone, "ls-files")
    assert result.behind == 2
