"""Bare @mention after the same author's image must load pixels, not filename stubs."""

from __future__ import annotations

import io
import logging
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oi_agent.config import Config, WatchTarget
from oi_agent.external.prepare import prepare_external_inputs
from oi_agent.external.records import STATUS_IMAGE
from oi_agent.opencode.paths import resolve_opencode_paths
from oi_agent.opencode.prompt import EXTERNAL_EVIDENCE_CONTRACT
from oi_agent.watch import attachments as watch_attachments
from oi_agent.watch.attachments import (
    author_follow_up_attachment_messages,
    gather_candidates,
    history_messages_for_attachments,
    unloaded_history_attachment_note,
)
from oi_agent.watch.discord_client import OIWatcher
from oi_agent.store import Store

BOT_ID = 99
CHANNEL_ID = 1559632120727605360


class _FakeAuthor:
    def __init__(self, user_id: int, name: str = "author") -> None:
        self.id = user_id
        self.display_name = name
        self.bot = False


class _FakeMention:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.display_name = "ultron"


class _FakeAttachment:
    def __init__(
        self,
        attachment_id: str,
        filename: str,
        *,
        size: int = 100,
        content_type: str = "image/png",
    ) -> None:
        self.id = attachment_id
        self.filename = filename
        self.size = size
        self.content_type = content_type
        self.url = f"https://cdn.discordapp.com/attachments/1/2/{filename}"


class _FakeMessage:
    def __init__(
        self,
        message_id: int,
        author: _FakeAuthor,
        *,
        content: str = "",
        attachments: list[_FakeAttachment] | None = None,
        mentions: list[_FakeMention] | None = None,
        created_at: datetime | None = None,
    ) -> None:
        self.id = message_id
        self.author = author
        self.content = content
        self.attachments = list(attachments or ())
        self.mentions = list(mentions or ())
        self.created_at = created_at or datetime.now(UTC)
        self.embeds = []
        self.referenced_message = None
        self.reference = None


def _png_bytes() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color=(4, 8, 16)).save(buffer, format="PNG")
    return buffer.getvalue()


def _watcher(tmp_path: Path) -> OIWatcher:
    cfg = Config()
    cfg.external_inputs = "read-only"
    cfg.image_inputs = "on"
    cfg.watches = [WatchTarget(CHANNEL_ID, str(tmp_path / "repo"), BOT_ID, 3)]
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


def _candidate_with_fetch(candidate, payload: bytes):
    async def fake_fetch() -> bytes:
        return payload

    return type(candidate)(
        attachment_id=candidate.attachment_id,
        source_message_id=candidate.source_message_id,
        filename=candidate.filename,
        declared_media_type=candidate.declared_media_type,
        declared_size=candidate.declared_size,
        fetch=fake_fetch,
    )


def test_bare_mention_loads_own_prior_image_from_history() -> None:
    """Bare @mention after the author's PNG must gather that history message."""
    author = _FakeAuthor(7, "Gopaul")
    image_time = datetime(2026, 3, 14, 17, 52, tzinfo=UTC)
    mention_time = image_time + timedelta(minutes=15)
    image_msg = _FakeMessage(
        100,
        author,
        content="here you go",
        attachments=[_FakeAttachment("img1", "New_1.png")],
        created_at=image_time,
    )
    mention_msg = _FakeMessage(
        101,
        author,
        content=f"<@{BOT_ID}>",
        mentions=[_FakeMention(BOT_ID)],
        created_at=mention_time,
    )
    history = history_messages_for_attachments(
        question="",
        burst_messages=[mention_msg],
        context_messages=[image_msg, mention_msg],
        reply_messages=[],
        bot_user_id=BOT_ID,
    )
    assert history == [image_msg]
    candidates = gather_candidates(
        burst_messages=[mention_msg],
        context_messages=history,
        reply_messages=[],
    )
    assert len(candidates) == 1
    assert candidates[0].filename == "New_1.png"


def test_other_author_image_not_pulled() -> None:
    author = _FakeAuthor(7)
    other = _FakeAuthor(8, "other")
    now = datetime.now(UTC)
    foreign = _FakeMessage(
        50,
        other,
        attachments=[_FakeAttachment("x", "secret.png")],
        created_at=now - timedelta(minutes=2),
    )
    own = _FakeMessage(
        51,
        author,
        content=f"<@{BOT_ID}>",
        mentions=[_FakeMention(BOT_ID)],
        created_at=now,
    )
    picked = author_follow_up_attachment_messages(
        burst_messages=[own],
        context_messages=[foreign, own],
        reply_messages=[],
        bot_user_id=BOT_ID,
    )
    assert picked == []


@pytest.mark.asyncio
async def test_look_at_this_mention_pulls_recent_own_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _png_bytes()

    async def fake_fetch(url: str) -> bytes:
        return png

    monkeypatch.setattr(watch_attachments, "fetch_attachment_bytes", fake_fetch)
    author = _FakeAuthor(7)
    image_msg = _FakeMessage(
        200,
        author,
        attachments=[_FakeAttachment("img2", "frame.png", size=len(png))],
        created_at=datetime.now(UTC) - timedelta(minutes=3),
    )
    mention_msg = _FakeMessage(
        201,
        author,
        content=f"<@{BOT_ID}> look at this",
        mentions=[_FakeMention(BOT_ID)],
        created_at=datetime.now(UTC),
    )
    watcher = _watcher(tmp_path)
    prepared = await watcher._prepare_external(
        "look at this",
        burst_messages=[mention_msg],
        context_messages=[image_msg, mention_msg],
        reply_messages=[],
        bot_user_id=BOT_ID,
    )
    assert len(prepared.inputs.attachments) == 1
    assert prepared.inputs.attachments[0].status == STATUS_IMAGE
    assert prepared.image_preparation is not None
    assert prepared.image_preparation.paths


@pytest.mark.asyncio
async def test_unloaded_attachment_prompt_note(tmp_path: Path) -> None:
    author = _FakeAuthor(7)
    image_msg = _FakeMessage(
        300,
        author,
        attachments=[_FakeAttachment("img3", "missing.png")],
        created_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    mention_msg = _FakeMessage(
        301,
        author,
        content=f"<@{BOT_ID}>",
        mentions=[_FakeMention(BOT_ID)],
        created_at=datetime.now(UTC),
    )
    note = unloaded_history_attachment_note(
        context_messages=[image_msg, mention_msg],
        burst_messages=[mention_msg],
        loaded_source_message_ids=set(),
        bot_user_id=BOT_ID,
    )
    assert note is not None
    assert "could not load" in note
    assert "host disk" in note
    assert "save or copy" in note
    assert "host disk" in EXTERNAL_EVIDENCE_CONTRACT
    assert "image_inputs=off" in EXTERNAL_EVIDENCE_CONTRACT
    assert "staging folders" in EXTERNAL_EVIDENCE_CONTRACT


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _plain_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("notes\n", encoding="utf-8")
    _git("init", "-q", str(repo))
    _git("-C", str(repo), "config", "user.email", "t@example.com")
    _git("-C", str(repo), "config", "user.name", "Test")
    _git("-C", str(repo), "add", "-A")
    _git("-C", str(repo), "commit", "-qm", "init")
    return repo


def _external_script(tmp_path: Path) -> Path:
    body = f'''#!{sys.executable}
import json, os, sqlite3, sys
if sys.argv[1:] == ["--version"]:
    print("opencode 2.0.5")
    sys.exit(0)
argv = sys.argv[1:]
real_run = not any("__schema_init__" in a for a in argv)
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
        connection.execute(
            "INSERT OR REPLACE INTO credential VALUES "
            "('cred-1', 'integration', 'label', 'dummy', NULL, NULL, 1, 1, 1)"
        )
        connection.commit()
    finally:
        connection.close()
if real_run:
    session = "session-img"
    print(json.dumps({{"type": "text", "timestamp": 0, "sessionID": session,
                      "part": {{"id": "p1", "messageID": "msg_final",
                               "sessionID": session, "type": "text",
                               "text": "[[OI_FINAL_ANSWER]]\\nOK",
                               "time": {{"start": 0, "end": 1}}}}}}))
'''
    path = tmp_path / "fake-opencode"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _seed_credentials() -> None:
    db_path = resolve_opencode_paths().data_home / "opencode" / "opencode.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = __import__("sqlite3").connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS credential "
            "(id TEXT PRIMARY KEY, integration_id TEXT, label TEXT, value TEXT, "
            "connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        connection.execute(
            "INSERT OR REPLACE INTO credential VALUES "
            "('cred-1', 'integration', 'label', 'dummy', NULL, NULL, 1, 1, 1)"
        )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_runner_external_run_logs_images_gt_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    _seed_credentials()
    png = _png_bytes()
    author = _FakeAuthor(7)
    image_msg = _FakeMessage(
        400,
        author,
        attachments=[_FakeAttachment("img4", "screen.png", size=len(png))],
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    mention_msg = _FakeMessage(
        401,
        author,
        content=f"<@{BOT_ID}>",
        mentions=[_FakeMention(BOT_ID)],
        created_at=datetime.now(UTC),
    )
    watcher = _watcher(tmp_path)
    watcher._cfg.opencode_binary = str(_external_script(tmp_path))
    watcher._cfg.opencode_model = "provider/model"
    watcher._cfg.opencode_timeout_seconds = 15
    prepared = await watcher._prepare_external(
        "",
        burst_messages=[mention_msg],
        context_messages=[image_msg, mention_msg],
        reply_messages=[],
        bot_user_id=BOT_ID,
    )
    candidates = gather_candidates(
        burst_messages=[mention_msg],
        context_messages=[image_msg],
        reply_messages=[],
    )
    assert len(candidates) == 1
    prepared = await prepare_external_inputs(
        "",
        [_candidate_with_fetch(candidates[0], png)],
    )
    repo = _plain_repo(tmp_path)
    caplog.set_level(logging.INFO)
    reply = await watcher._runner.run_external_evidence(
        prepared,
        target=WatchTarget(CHANNEL_ID, str(repo)),
        author="Gopaul",
        question="",
        excerpt="history shows New_1.png",
    )
    try:
        assert any(
            "images=1" in record.getMessage()
            for record in caplog.records
            if record.name == "oi_agent.opencode.runner"
        )
    finally:
        prepared.close()
