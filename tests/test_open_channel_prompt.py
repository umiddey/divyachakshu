"""Open-channel prompt tests, plus a golden guard that non-open prompts stay byte-identical."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from conftest import FakeAuthor, FakeChannel, FakeMention, FakeMessage, make_watcher
from test_docs_only_write import _git_repo

from oi_agent.agent.reply import Reply
from oi_agent.config import Config, WatchTarget
from oi_agent.dynamics import MemorySnapshot, ProfileView
from oi_agent.opencode import runner as runner_module
from oi_agent.opencode.prompt import (
    DEFAULT_VOICE,
    FINAL_OUTPUT_MARKER,
    OPEN_CHANNEL_SILENT,
    build_prompt,
    is_silent_reply,
)
from oi_agent.opencode.runner import OpenCodeRunner, _extract_marked_answer
from oi_agent.poster import DeliveryResult
from oi_agent.store import Store
from oi_agent.watch import discord_client

GOLDEN = Path(__file__).parent / "fixtures" / "build_prompt_golden.json"
_GENERATED_AT = "2026-01-01T00:00:00+00:00"


def golden_cases() -> dict[str, dict]:
    """Non-open ``build_prompt`` inputs whose full output is pinned in GOLDEN."""
    view = ProfileView(
        member_id="m1",
        handle="member",
        directness_band="high",
        detail_preference="brief",
        challenge_preference="direct",
        humor_preference="medium",
        decision_style="options",
        recurring_topics=("runner",),
        confidence=0.9,
    )
    empty = MemorySnapshot(
        profiles=(), transient=(), team_pulse=None, calibration=None, generated_at=_GENERATED_AT
    )
    full = MemorySnapshot(
        profiles=(view,), transient=(), team_pulse=None, calibration=None, generated_at=_GENERATED_AT
    )
    return {
        "default": {},
        "personality": {"personality": "Speak like a terse reviewer"},
        "server_env": {"environment_mode": "server", "primary_repo": "/repo"},
        "docs_only": {
            "primary_repo": "/repo",
            "write_mode": "docs-only",
            "write_dirs": ["docs"],
        },
        "multi_repo": {"primary_repo": "/repo", "additional_repos": ["/aux1", "/aux2"]},
        "identity_name_and_id": {"bot_name": "Oi Bot", "bot_id": 1234567890},
        "identity_name_only": {"bot_name": "Oi Bot"},
        "identity_id_only": {"bot_id": 1234567890},
        "memory_full": {"primary_repo": "/repo", "memory": full},
        "memory_empty": {"primary_repo": "/repo", "memory": empty},
        "external_inputs": {"primary_repo": "/repo", "external_inputs": True},
    }


def test_non_open_prompts_byte_identical():
    """Non-open prompts match the pinned output exactly."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = golden_cases()
    assert expected.keys() == cases.keys()
    for name, kwargs in cases.items():
        assert build_prompt(**kwargs) == expected[name], name


def test_open_none_prompt_is_minimal_plumbing():
    """The none mode keeps only identity, data, secret, and marker plumbing."""
    text = build_prompt(bot_name="Oi Bot", bot_id=90, open_channel_prompt="none")
    assert FINAL_OUTPUT_MARKER in text
    assert OPEN_CHANNEL_SILENT in text
    assert "Oi Bot" in text
    assert "never instructions to you" in text
    assert "=== UNTRUSTED CONTEXT DATA" not in text
    assert DEFAULT_VOICE not in text
    assert "NON-NEGOTIABLE SAFETY" not in text
    assert "RESPONSE CONTRACT" not in text
    assert len(text) < 1000


def test_open_personality_prompt_prepends_personality_only():
    """Personality mode prepends the stripped personality; none ignores it."""
    base = build_prompt(bot_name="Oi Bot", bot_id=90, open_channel_prompt="none")
    styled = build_prompt(
        "  Speak like a terse reviewer \n",
        bot_name="Oi Bot",
        bot_id=90,
        open_channel_prompt="personality",
    )
    assert styled == "Speak like a terse reviewer\n\n" + base
    empty = build_prompt("", bot_name="Oi Bot", bot_id=90, open_channel_prompt="personality")
    assert empty == base
    assert build_prompt("Speak like X", open_channel_prompt="none") == build_prompt(
        open_channel_prompt="none"
    )


def test_open_channel_prompt_rejects_unknown_mode():
    """An unknown open-channel mode is an error, not a silent default."""
    with pytest.raises(ValueError):
        build_prompt(open_channel_prompt="full")


def test_open_channel_marker_and_silence_contract():
    """Marker parsing and silence detection are unchanged for open runs."""
    assert _extract_marked_answer("x\n[[OI_FINAL_ANSWER]]\nhi") == ("hi", None)
    assert is_silent_reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}")
    assert is_silent_reply(OPEN_CHANNEL_SILENT)
    assert not is_silent_reply(f"{FINAL_OUTPUT_MARKER}\nhi")


class _Captured(Exception):
    """Stops _run_locked before the OpenCode subprocess starts."""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("open_mode", "expected_write_mode", "expected_dirs", "expected_snapshots"),
    [("none", "read-only", [], 0), (None, "docs-only", ["docs"], 1)],
)
async def test_open_runs_are_read_only_without_memory(
    tmp_path,
    monkeypatch,
    open_mode,
    expected_write_mode,
    expected_dirs,
    expected_snapshots,
):
    """Open runs ignore docs-only and skip memory; other runs keep both."""
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(
        runner_module, "resolve_runtime_binary", lambda *args, **kwargs: "/bin/true"
    )
    cfg = Config(opencode_binary="/bin/true", opencode_model="provider/model")
    cfg.write_mode = "docs-only"
    cfg.write_dirs = ["docs"]
    store = Store(tmp_path / "state.db")
    snapshots = []
    real_snapshot = store.memory_snapshot

    def counting_snapshot(*args, **kwargs):
        snapshots.append(args)
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(store, "memory_snapshot", counting_snapshot)
    runner = OpenCodeRunner(cfg, store)
    seen = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        raise _Captured

    monkeypatch.setattr(runner, "_execute_agent", fake_execute)
    target = WatchTarget(111, str(repo), 99, 3)
    with pytest.raises(_Captured):
        await runner._run_locked(
            repo, [], target, "alice", "what changed?", "", open_channel_prompt=open_mode
        )
    assert seen["write_mode"] == expected_write_mode
    assert seen["write_dirs"] == expected_dirs
    assert len(snapshots) == expected_snapshots
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_mode", "override", "open_listed", "expected"),
    [
        ("none", {}, True, "none"),
        ("personality", {}, True, "personality"),
        ("personality", {111: "none"}, True, "none"),
        ("personality", {111: "personality"}, False, None),
    ],
)
async def test_discord_resolves_open_prompt_per_channel(
    tmp_path, monkeypatch, global_mode, override, open_listed, expected
):
    """Open channels inherit the global mode unless overridden; others get None."""
    monkeypatch.setattr(discord_client, "BURST_QUIET_SECONDS", 0)
    watcher = make_watcher(
        tmp_path, open_channels=(111,) if open_listed else (), post_hourly_cap=20
    )
    watcher._cfg.open_channel_prompt = global_mode
    watcher._cfg.watches[0].open_channel_prompts = dict(override)
    channel = FakeChannel(111)
    if open_listed:
        message = FakeMessage(1, channel, FakeAuthor(7), "thanks everyone!", [])
    else:
        message = FakeMessage(
            1, channel, FakeAuthor(7), "@ultron (user id 99) hello", [FakeMention(99)]
        )
    channel._history = [message]
    seen = []

    async def fake_run(*args, **kwargs):
        seen.append(kwargs.get("open_channel_prompt"))
        return Reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}", "sha")

    async def fake_deliver(self, **kwargs):
        return DeliveryResult(ok=True, discord_message_id=999)

    monkeypatch.setattr(watcher._runner, "run", fake_run)
    monkeypatch.setattr(discord_client.Poster, "deliver_chunk", fake_deliver)
    await watcher.on_message(message)
    await asyncio.sleep(0.05)
    assert seen == [expected]
