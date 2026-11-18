"""Tests for the open-channel silence contract (P6).

Covers the prompt-side OPEN CHANNEL block, the ``[[OI_SILENT]]`` detection
helper, and the store-side silent completion that ends jobs ``done`` without
any delivery row.
"""

from oi_agent.opencode.prompt import (
    FINAL_OUTPUT_MARKER,
    OPEN_CHANNEL_SILENT,
    build_prompt,
    is_silent_reply,
)
from oi_agent.store import Store


def test_build_prompt_open_channel_block_only_when_enabled():
    """The OPEN CHANNEL contract and silence token appear only for open runs."""
    default = build_prompt()
    assert "OPEN CHANNEL MODE:" not in default
    assert OPEN_CHANNEL_SILENT not in default
    enabled = build_prompt(open_channel=True)
    assert "OPEN CHANNEL MODE:" in enabled
    assert OPEN_CHANNEL_SILENT in enabled
    assert FINAL_OUTPUT_MARKER in enabled


def test_prompt_silent_token_detection_exact():
    """A bare token after the final marker is silence, with or without marker."""
    assert is_silent_reply(f"{FINAL_OUTPUT_MARKER}\n{OPEN_CHANNEL_SILENT}")
    assert is_silent_reply(OPEN_CHANNEL_SILENT)


def test_prompt_silent_token_detection_whitespace():
    """Surrounding whitespace around the token still counts as silence."""
    assert is_silent_reply(f"{FINAL_OUTPUT_MARKER}\n\n  {OPEN_CHANNEL_SILENT}  \n")
    assert is_silent_reply(f"  {OPEN_CHANNEL_SILENT}\t")


def test_prompt_silent_token_detection_rejects_embedded_or_absent_token():
    """A token inside a sentence, or any other answer, is never silence."""
    assert not is_silent_reply(
        f"{FINAL_OUTPUT_MARKER} I would say {OPEN_CHANNEL_SILENT} mid-sentence, but no."
    )
    assert not is_silent_reply(f"{FINAL_OUTPUT_MARKER}\nhere is the real answer")
    assert not is_silent_reply("")
    # A token before the final marker is narration, not the answer.
    assert not is_silent_reply(
        f"considering {OPEN_CHANNEL_SILENT} {FINAL_OUTPUT_MARKER} actual answer"
    )


def test_silent_store_complete_jobs_silent_marks_done_without_delivery(tmp_path):
    """Silent completion ends leased jobs done with no batch or chunk rows."""
    store = Store(tmp_path / "state.db")
    try:
        store.admit_mention_job(11, 42, 42, tmp_path / "repo")
        claimed = store.claim_next_conversation_jobs()
        assert claimed is not None
        conv_id, jobs = claimed
        assert conv_id == 42
        assert [j["source_message_id"] for j in jobs] == [11]

        store.complete_jobs_silent([11])

        row = store._conn.execute(
            "SELECT status, batch_id, lease_until FROM mention_jobs "
            "WHERE source_message_id = 11"
        ).fetchone()
        assert row == ("done", None, 0)
        assert store._conn.execute("SELECT COUNT(*) FROM delivery_batches").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM outbound_chunks").fetchone()[0] == 0

        # Repeating a silent completion stays idempotent and adds no delivery.
        store.complete_jobs_silent([11])
        assert store._conn.execute(
            "SELECT status FROM mention_jobs WHERE source_message_id = 11"
        ).fetchone()[0] == "done"
        assert store._conn.execute("SELECT COUNT(*) FROM delivery_batches").fetchone()[0] == 0
    finally:
        store.close()


def test_silent_store_complete_jobs_silent_empty_ids_is_noop(tmp_path):
    """An empty id list touches nothing."""
    store = Store(tmp_path / "state.db")
    try:
        store.complete_jobs_silent([])
        assert store._conn.execute("SELECT COUNT(*) FROM mention_jobs").fetchone()[0] == 0
    finally:
        store.close()
