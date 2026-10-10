"""Behavior and security tests for attachment caps, image capability, cleanup.

Correction 6: operator count/byte caps hold against ACTUAL downloaded bytes
and overflow is reported, never pre-sliced away; history attachments are
opt-in. Correction 8: the validated image-capability setting decides whether
pixels are attached, and nonvision runs demote images to honest unsupported
statuses. Correction 10: OI temp roots carry owner markers and only ever
sweep provably abandoned roots.
"""

import asyncio
import io
import os
from pathlib import Path

import pytest

from oi_agent._tempowner import mark_owner, sweep_abandoned_temp_roots
from oi_agent.config import Config
from oi_agent.external.images import ImagePreparation, PreparedImage
from oi_agent.external.prepare import AttachmentCandidate, PreparedExternal, prepare_external_inputs
from oi_agent.external.records import (
    MAX_ATTACHMENT_BYTES,
    STATUS_IMAGE,
    STATUS_LIMIT,
    STATUS_TEXT,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    PreparedInputs,
)
from oi_agent.opencode.runner import (
    OpenCodeRunner,
    _demote_images_without_vision,
)
from oi_agent.watch.attachments import (
    APPROVED_CDN_HOSTS,
    context_attachments_requested,
    gather_candidates,
    is_approved_attachment_url,
)


class _FakeAttachment:
    """Minimal discord.py-shaped attachment object."""

    def __init__(self, attachment_id: str, filename: str, size: int = 10,
                 content_type: str = "text/plain", url: str = "") -> None:
        self.id = attachment_id
        self.filename = filename
        self.size = size
        self.content_type = content_type
        self.url = url or f"https://cdn.discordapp.com/attachments/1/2/{filename}"


class _FakeMessage:
    def __init__(self, message_id: int, attachments: list[_FakeAttachment]) -> None:
        self.id = message_id
        self.attachments = attachments


def _candidate(attachment_id: str, payload: bytes, declared: int | None = None,
               source: int = 1) -> AttachmentCandidate:
    return AttachmentCandidate(
        attachment_id=attachment_id,
        source_message_id=source,
        filename=f"{attachment_id}.txt",
        declared_media_type="text/plain",
        declared_size=declared if declared is not None else len(payload),
        fetch=lambda payload=payload: asyncio.sleep(0, result=payload),
    )


# --- correction 6: caps end-to-end -------------------------------------------


@pytest.mark.asyncio
async def test_operator_count_cap_records_overflow_not_silence() -> None:
    prepared = await prepare_external_inputs(
        "hello",
        [_candidate(f"a{i}", b"content") for i in range(4)],
        max_attachments=2,
    )
    records = prepared.inputs.attachments
    assert len(records) == 4, "every candidate gets an honest record"
    extracted = [r for r in records if r.status == STATUS_TEXT]
    limited = [r for r in records if r.status == STATUS_LIMIT]
    assert len(extracted) == 2
    assert len(limited) == 2
    assert any("2-attachment limit" in note for r in limited for note in r.limitations)


@pytest.mark.asyncio
async def test_lying_declared_size_cannot_smuggle_oversized_bytes() -> None:
    """A small declared size must not bypass the per-item cap on download."""
    payload = b"x" * (MAX_ATTACHMENT_BYTES + 1)
    prepared = await prepare_external_inputs(
        "hello",
        [_candidate("liar", payload, declared=10)],
    )
    record = prepared.inputs.attachments[0]
    assert record.status == STATUS_TOO_LARGE
    assert "downloaded size" in record.limitations[0]


@pytest.mark.asyncio
async def test_aggregate_byte_cap_enforced_on_actual_bytes() -> None:
    """Three max-size files bust the 16 MiB aggregate on ACTUAL bytes."""
    chunk = b"a" * MAX_ATTACHMENT_BYTES
    prepared = await prepare_external_inputs(
        "hello", [_candidate(f"n{i}", chunk) for i in range(3)]
    )
    statuses = [r.status for r in prepared.inputs.attachments]
    assert statuses[0] == STATUS_TEXT
    assert statuses[1] == STATUS_TEXT
    assert statuses[2] == STATUS_TOO_LARGE
    assert "aggregate" in prepared.inputs.attachments[2].limitations[0]


def test_gather_candidates_keeps_every_attachment_no_preslice() -> None:
    """All burst sources, replies, and history attachments are candidates."""
    burst = [_FakeMessage(1, [_FakeAttachment("11", "a.txt")])]
    history = [
        _FakeMessage(2, [_FakeAttachment("22", "b.txt")]),
        _FakeMessage(3, [_FakeAttachment("33", "c.txt")]),
    ]
    replies = [_FakeMessage(4, [_FakeAttachment("44", "d.txt")])]
    candidates = gather_candidates(
        burst_messages=burst, context_messages=history, reply_messages=replies
    )
    assert {c.source_message_id for c in candidates} == {1, 2, 3, 4}


def test_duplicate_attachment_ids_deduplicate() -> None:
    same = _FakeAttachment("11", "a.txt")
    candidates = gather_candidates(
        burst_messages=[_FakeMessage(1, [same])],
        context_messages=[_FakeMessage(2, [same])],
        reply_messages=[],
    )
    assert len(candidates) == 1


# --- correction 6: history attachments are opt-in ----------------------------


def test_context_attachments_opt_in_requires_explicit_reference() -> None:
    assert context_attachments_requested("what does the file above contain")
    assert context_attachments_requested("read that screenshot from earlier")
    assert context_attachments_requested("summarize the attachments posted before my question")
    assert not context_attachments_requested("summarize report.pdf")
    assert not context_attachments_requested("what's the latest news about Python")
    assert not context_attachments_requested("search the web for files about caching")


# --- correction 6: approved CDN policy ---------------------------------------


def test_only_approved_discord_cdn_hosts_are_attachment_urls() -> None:
    for host in APPROVED_CDN_HOSTS:
        assert is_approved_attachment_url(f"https://{host}/attachments/1/2/x.png")
    assert not is_approved_attachment_url("https://evil.example/x.png")
    assert not is_approved_attachment_url("http://cdn.discordapp.com/x.png")
    assert not is_approved_attachment_url("https://cdn.discordapp.com.evil.example/x.png")
    assert not is_approved_attachment_url("")


# --- correction 7: honest rejected-image records -----------------------------


@pytest.mark.asyncio
async def test_malformed_image_bytes_get_explicit_unsupported_record() -> None:
    png_magic = b"\x89PNG\r\n\x1a\n" + b"garbage-that-is-not-a-png"
    prepared = await prepare_external_inputs("hello", [_candidate("img", png_magic)])
    record = prepared.inputs.attachments[0]
    assert record.kind == "image"
    assert record.status == STATUS_UNSUPPORTED
    assert "not usable" in record.limitations[0]


@pytest.mark.asyncio
async def test_oversized_pixel_image_is_rejected_by_budget(tmp_path: Path) -> None:
    """A real PNG past the pixel budget is refused, never attached."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (3600, 3600), color=(1, 2, 3)).save(buffer, format="PNG")
    assert 3600 * 3600 > 12_000_000
    prepared = await prepare_external_inputs("hello", [_candidate("big", buffer.getvalue())])
    record = prepared.inputs.attachments[0]
    assert record.kind == "image"
    assert record.status == STATUS_UNSUPPORTED


@pytest.mark.asyncio
async def test_valid_image_prepares_and_records_native_file(tmp_path: Path) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color=(9, 9, 9)).save(buffer, format="PNG")
    prepared = await prepare_external_inputs("hello", [_candidate("ok", buffer.getvalue())])
    record = prepared.inputs.attachments[0]
    assert record.status == STATUS_IMAGE
    assert prepared.image_preparation is not None
    assert prepared.image_preparation.paths[0].exists()


# --- correction 8: image capability setting ----------------------------------


def _prepared_with_image(tmp_path: Path) -> PreparedExternal:
    prepared = PreparedExternal()
    path = tmp_path / "image-1.png"
    path.write_bytes(b"pixels")
    preparation = ImagePreparation(images=[
        PreparedImage(label="message 1: pic.png", media_type="image/png", path=path),
    ])
    prepared.image_preparation = preparation
    prepared.inputs.attachments.append(
        _image_record()
    )
    prepared.inputs.attachments.append(
        type(prepared.inputs.attachments[0])(
            attachment_id="doc", source_message_id=1, filename="notes.txt",
            declared_media_type="text/plain", declared_size=5,
            kind="document", status=STATUS_TEXT,
        )
    )
    return prepared


def _image_record():
    from oi_agent.external.records import AttachmentInput

    return AttachmentInput(
        attachment_id="native-image-1", source_message_id=0,
        filename="image-1.png", declared_media_type="image/png",
        declared_size=6, kind="image", status=STATUS_IMAGE,
    )


def test_nonvision_mode_attaches_no_pixels_and_demotes_images(tmp_path: Path) -> None:
    cfg = Config()
    assert cfg.image_inputs == "off", "off is the conservative default"
    runner = OpenCodeRunner(cfg)
    prepared = _prepared_with_image(tmp_path)
    files = runner._vision_attachments(prepared)
    assert files == []
    image_records = [r for r in prepared.inputs.attachments if r.kind == "image"]
    assert image_records and all(r.status == STATUS_UNSUPPORTED for r in image_records)
    assert any("visual understanding" in note
               for r in image_records for note in r.limitations)
    # Independent text/doc parts are untouched and still usable.
    docs = [r for r in prepared.inputs.attachments if r.kind == "document"]
    assert docs and docs[0].status == STATUS_TEXT


def test_vision_mode_attaches_real_files(tmp_path: Path) -> None:
    cfg = Config()
    cfg.image_inputs = "on"
    runner = OpenCodeRunner(cfg)
    prepared = _prepared_with_image(tmp_path)
    files = runner._vision_attachments(prepared)
    assert files == [tmp_path / "image-1.png"]
    assert prepared.inputs.attachments[0].status == STATUS_IMAGE


def test_demote_helper_leaves_non_image_records_alone(tmp_path: Path) -> None:
    prepared = _prepared_with_image(tmp_path)
    before = [r.status for r in prepared.inputs.attachments if r.kind == "document"]
    _demote_images_without_vision(prepared)
    after = [r.status for r in prepared.inputs.attachments if r.kind == "document"]
    assert before == after


# --- correction 10: owned temp namespaces ------------------------------------


def test_prepared_inputs_and_native_roots_carry_owner_markers(tmp_path: Path) -> None:
    inputs = PreparedInputs()
    workdir = inputs.workdir
    assert (workdir / "owner.json").exists()
    inputs.close()
    assert not workdir.exists()

    from oi_agent.opencode.native import DisposableNativeRoot

    with DisposableNativeRoot(tmp_path / "native") as root:
        assert (root.base / "owner.json").exists()


def test_sweep_removes_only_dead_owner_roots(tmp_path: Path) -> None:
    dead = tmp_path / "oi-native-dead"
    dead.mkdir()
    (dead / "owner.json").write_text('{"pid": 999999, "boot": "boot-a", "created": 1}')

    live = tmp_path / "oi-native-live"
    live.mkdir()
    (live / "owner.json").write_text(
        '{"pid": %d, "boot": "boot-a", "created": 1}' % os.getpid()
    )

    foreign = tmp_path / "oi-native-foreign"
    foreign.mkdir()

    other_boot = tmp_path / "oi-external-oldboot"
    other_boot.mkdir()
    (other_boot / "owner.json").write_text('{"pid": 999999, "boot": "boot-zzz", "created": 1}')

    removed = sweep_abandoned_temp_roots(
        tmp_dir=tmp_path, current_boot="boot-a", alive=lambda pid: pid == os.getpid()
    )
    assert not dead.exists(), "dead-owner root removed"
    assert live.exists(), "live-owner root kept"
    assert foreign.exists(), "markerless root never swept"
    assert other_boot.exists(), "other-boot root never swept"
    assert removed == 1

    # A sweep that considers every pid alive removes nothing.
    assert sweep_abandoned_temp_roots(
        tmp_dir=tmp_path, current_boot="boot-a", alive=lambda pid: True
    ) == 0


def test_real_owner_marker_sweeps_after_death(tmp_path: Path) -> None:
    """A root marked by THIS process survives; a marker naming a dead pid dies."""
    import os

    mine = tmp_path / "oi-native-mine"
    mine.mkdir()
    mark_owner(mine)
    assert sweep_abandoned_temp_roots(
        tmp_dir=tmp_path, alive=lambda pid: pid == os.getpid()
    ) == 0
    assert mine.exists()

    gone = tmp_path / "oi-external-gone"
    gone.mkdir()
    (gone / "owner.json").write_text(
        '{"pid": 999999, "boot": "x", "created": 1}'
    )
    import json as _json

    boot = _json.loads((mine / "owner.json").read_text())["boot"]
    (gone / "owner.json").write_text(
        _json.dumps({"pid": 999999, "boot": boot, "created": 1})
    )
    assert sweep_abandoned_temp_roots(
        tmp_dir=tmp_path, alive=lambda pid: pid == os.getpid()
    ) == 1
    assert not gone.exists()
