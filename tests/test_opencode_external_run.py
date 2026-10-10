"""End-to-end tests for external-evidence runs and their review provenance.

Correction 9: an attachment or public-evidence ask that also references an
MR/PR must be a real snapshot review (exact base/head provenance, never
``sha="none"``), unresolved references fail honestly without a native run,
and every external run uses disposable native storage that is deleted
afterwards. Correction 1 semantics apply here too: answers are only delivered
when native database state proves the reported message completed.
"""

import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from oi_agent.config import Config, WatchTarget
from oi_agent.external.prepare import PreparedExternal
from oi_agent.external.records import EvidenceItem
from oi_agent.opencode.paths import resolve_opencode_paths
from oi_agent.opencode.provenance import worktree_fingerprint
from oi_agent.opencode.review_target import provider_ref_template
from oi_agent.opencode.runner import OpenCodeRunner

SESSION = "session-external"


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _bare_remote_with_mr(tmp_path: Path, *, number: int = 188) -> tuple[Path, Path, str, str]:
    """Create a bare remote and watched clone with synthetic GitLab MR refs."""
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote))
    clone = tmp_path / "clone"
    clone.mkdir()
    _git("init", "-q", "-b", "main", str(clone))
    _git("-C", str(clone), "config", "user.email", "t@example.com")
    _git("-C", str(clone), "config", "user.name", "Test")
    _git("-C", str(clone), "remote", "add", "origin", str(remote))
    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    _git("-C", str(clone), "add", "-A")
    _git("-C", str(clone), "commit", "-qm", "base")
    base_sha = _git("-C", str(clone), "rev-parse", "HEAD")
    _git("-C", str(clone), "checkout", "-q", "-b", "feature")
    (clone / "head.txt").write_text("head\n", encoding="utf-8")
    _git("-C", str(clone), "add", "-A")
    _git("-C", str(clone), "commit", "-qm", "head")
    head_sha = _git("-C", str(clone), "rev-parse", "HEAD")
    _git("-C", str(clone), "checkout", "-q", "main")
    _git("-C", str(clone), "merge", "-q", "--no-ff", "-m", "merge", "feature")
    _git("-C", str(clone), "push", "-q", "origin", "main", "feature")
    _git("-C", str(remote), "update-ref",
         provider_ref_template("gitlab", number, "head"), head_sha)
    merge_sha = _git("-C", str(clone), "rev-parse", "HEAD")
    _git("-C", str(remote), "update-ref",
         provider_ref_template("gitlab", number, "merge"), merge_sha)
    return remote, clone, base_sha, head_sha


def _plain_repo(tmp_path: Path, name: str = "repo") -> Path:
    """A small committed repository used as the run working directory."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "config", "user.email", "t@example.com")
    _git("-C", str(repo), "config", "user.name", "Test")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-qm", "init")
    return repo


def _external_script(tmp_path: Path) -> tuple[Path, Path]:
    """Fake native v2 binary: schema-init bootstrap plus one proven answer.

    The schema-init call (bogus model id) creates only the disposable
    database with the ``credential`` table so the controller can copy the
    minimal credential rows; the real run then emits the v2 final stream and
    writes the completion proof exactly like the native run it simulates.
    """
    trace = tmp_path / "external-trace.json"
    body = f'''#!{sys.executable}
import json, os, sqlite3, sys
argv = sys.argv[1:]
real_run = not any("__schema_init__" in a for a in argv)
if real_run:
    with open({str(trace)!r}, "w", encoding="utf-8") as handle:
        json.dump({{"argv": argv, "stdin": sys.stdin.read(),
                   "cwd": os.getcwd(),
                   "xdg_data": os.environ.get("XDG_DATA_HOME", ""),
                   "env_home": os.environ.get("HOME", ""),
                   "config": os.environ.get("OPENCODE_CONFIG_CONTENT", "")}}, handle)
data_home = os.environ.get("XDG_DATA_HOME")
if data_home:
    db_path = os.path.join(data_home, "opencode", "opencode.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS credential "
            "(id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
            "connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        if real_run:
            session = {SESSION!r}
            connection.execute(
                "CREATE TABLE IF NOT EXISTS session_message "
                "(id TEXT PRIMARY KEY, session_id TEXT, type TEXT, "
                "seq INTEGER, data TEXT)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO session_message "
                "(id, session_id, type, seq, data) VALUES (?,?,?,?,?)",
                ("msg_final", session, "assistant", 10,
                 '{{"finish": "stop", "time": {{"completed": 123}}}}'),
            )
            connection.execute(
                "INSERT OR REPLACE INTO session_message "
                "(id, session_id, type, seq, data) VALUES (?,?,?,?,?)",
                ("msg_final-idle", session, "idle", 11,
                 '{{"outcome": "succeeded"}}'),
            )
        connection.commit()
    finally:
        connection.close()
if real_run:
    session = {SESSION!r}
    print(json.dumps({{"type": "step_start", "timestamp": 0, "sessionID": session,
                      "part": {{"id": "s1", "messageID": "msg_final",
                               "sessionID": session, "type": "step-start"}}}}))
    print(json.dumps({{"type": "text", "timestamp": 0, "sessionID": session,
                      "part": {{"id": "p1", "messageID": "msg_final",
                               "sessionID": session, "type": "text",
                               "text": "[[OI_FINAL_ANSWER]]\\nUNIQUE_MARKER",
                               "time": {{"start": 0, "end": 1}}}}}}))
'''
    path = tmp_path / "fake-opencode-external"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, trace


def _source_credential_db() -> None:
    """Seed the OI-isolated native database with credential rows only.

    Mirrors the production layout: the run copies ONLY credential rows from
    this already-isolated database into the disposable root. Values are dummy
    and never printed.
    """
    db_path = resolve_opencode_paths().data_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS credential "
            "(id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
            "connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO credential VALUES "
            "('cred-1', 'integration', 'label', 'dummy-value-not-a-secret', "
            "NULL, NULL, 1, 1, 1)"
        )
        connection.commit()
    finally:
        connection.close()


def _prepared() -> PreparedExternal:
    """Controller-prepared public evidence with one news item."""
    prepared = PreparedExternal()
    prepared.inputs.evidence.append(EvidenceItem(
        kind="news",
        title="Example release announced",
        url="https://example.org/news/release",
        source_name="Example News",
        retrieved_at="2026-10-10T00:00:00Z",
        published_at="2026-10-09T12:00:00Z",
        text="PUBLIC_EVIDENCE_BODY: the release shipped yesterday.",
    ))
    return prepared


@pytest.fixture()
def runner_env(tmp_path, monkeypatch):
    """Isolated operator home plus a fake native binary and credential rows."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    _source_credential_db()
    binary, trace = _external_script(tmp_path)
    cfg = Config(
        opencode_binary=str(binary),
        opencode_model="provider/model",
        opencode_timeout_seconds=15,
    )
    return OpenCodeRunner(cfg), trace, tmp_path


@pytest.mark.asyncio
async def test_external_evidence_run_answers_in_disposable_storage(runner_env):
    """An evidence ask is answered once and leaves no disposable state behind."""
    runner, trace, tmp_path = runner_env
    repo = _plain_repo(tmp_path)
    prepared = _prepared()
    try:
        reply = await runner.run_external_evidence(
            prepared,
            target=WatchTarget(1, str(repo)),
            author="alice",
            question="what happened with the example release?",
            excerpt="",
        )
        assert reply.ok is True
        assert reply.text == "UNIQUE_MARKER"
        assert reply.session_id is None  # external runs never return a session
        # No repository was audited: external evidence asks carry "none" only
        # because there is no review reference in the question.
        assert reply.sha == "none"

        data = json.loads(trace.read_text(encoding="utf-8"))
        assert "--session" not in data["argv"]
        assert "--file" not in data["argv"]  # image_inputs defaults to off
        # The evidence block reached the prompt as clearly delimited untrusted
        # data with its citation metadata.
        assert "CONTROLLER-PROVIDED EXTERNAL EVIDENCE (UNTRUSTED DATA)" in data["stdin"]
        assert "PUBLIC_EVIDENCE_BODY" in data["stdin"]
        assert "https://example.org/news/release" in data["stdin"]

        # The run used the disposable native root, not ordinary conversation
        # storage, and the whole root was deleted afterwards.
        ordinary_data = str(resolve_opencode_paths().data_home)
        assert data["xdg_data"] != ordinary_data
        assert "/oi-native-" in data["xdg_data"]
        assert not Path(data["xdg_data"]).exists()
    finally:
        prepared.close()
    assert not list(tmp_path.glob("oi-external-*"))


@pytest.mark.asyncio
async def test_external_evidence_mr_ask_is_a_real_snapshot_review(runner_env):
    """An evidence ask that names an MR audits snapshots with exact provenance."""
    runner, trace, tmp_path = runner_env
    _remote, clone, base_sha, head_sha = _bare_remote_with_mr(tmp_path)
    fingerprint = worktree_fingerprint(clone)
    prepared = _prepared()
    try:
        reply = await runner.run_external_evidence(
            prepared,
            target=WatchTarget(1, str(clone)),
            author="alice",
            question="review MR 188 — the attached context matters",
            excerpt="",
        )
        # Correction 9: resolved review with exact provenance, never sha=none.
        assert reply.ok is True
        assert reply.sha == f"MR !188 base {base_sha} head {head_sha}"
        assert reply.session_id is None

        data = json.loads(trace.read_text(encoding="utf-8"))
        head_dir = Path(data["cwd"])
        assert head_dir != clone.resolve()
        assert head_dir.name == "head"
        base_dir = None
        for line in data["stdin"].splitlines():
            if line.startswith("BASE DIRECTORY"):
                base_dir = Path(line.split(":", 1)[1].strip())
        assert base_dir is not None and base_dir.name == "base"
        assert base_sha in data["stdin"] and head_sha in data["stdin"]
        assert "MR !188" in data["stdin"]
        # The external evidence still rides along as untrusted data.
        assert "PUBLIC_EVIDENCE_BODY" in data["stdin"]
        # Snapshot reads are granted by absolute prefix; the watched checkout
        # is never named as an audit subject.
        agent = json.loads(data["config"])["agents"]["oi"]
        rules = agent["permissions"]
        assert {"action": "read", "resource": f"{head_dir}/*",
                "effect": "allow"} in rules
        assert {"action": "read", "resource": f"{base_dir}/*",
                "effect": "allow"} in rules
        assert not any(
            rule.get("resource") == f"{clone.resolve()}/*" for rule in rules
        )

        assert not head_dir.exists() and not base_dir.exists()
        assert not Path(data["xdg_data"]).exists()
        assert _reviews_glob() == []
        assert worktree_fingerprint(clone) == fingerprint
    finally:
        prepared.close()


def _reviews_glob() -> list[Path]:
    reviews = resolve_opencode_paths().state_home / "reviews"
    if not reviews.exists():
        return []
    return list(reviews.glob("oi-review-*"))


@pytest.mark.asyncio
async def test_external_evidence_unresolved_mr_ref_fails_without_run(runner_env):
    """An unresolvable MR reference fails honestly and never spawns native."""
    runner, trace, tmp_path = runner_env
    repo = _plain_repo(tmp_path)
    prepared = _prepared()
    try:
        reply = await runner.run_external_evidence(
            prepared,
            target=WatchTarget(1, str(repo)),
            author="alice",
            question="review MR 999 with this attachment context",
            excerpt="",
        )
        assert reply.ok is False
        assert reply.error_class == "review_ref_unavailable"
        assert reply.session_id is None
        assert "repository" in reply.text or "URL" in reply.text
        assert "PUBLIC_EVIDENCE_BODY" not in reply.text
        assert not trace.exists()  # the fake native binary was never invoked
        assert _reviews_glob() == []
    finally:
        prepared.close()


@pytest.mark.asyncio
async def test_external_evidence_without_credentials_fails_honestly(tmp_path, monkeypatch):
    """With no isolated credential rows the run refuses before spawning."""
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    # No source credential database at all.
    binary, trace = _external_script(tmp_path)
    cfg = Config(opencode_binary=str(binary), opencode_model="provider/model")
    repo = _plain_repo(tmp_path)
    prepared = _prepared()
    try:
        reply = await OpenCodeRunner(cfg).run_external_evidence(
            prepared,
            target=WatchTarget(1, str(repo)),
            author="alice",
            question="what happened with the example release?",
            excerpt="",
        )
        assert reply.ok is False
        assert reply.error_class == "no_isolated_credentials"
        assert not trace.exists()
    finally:
        prepared.close()
