"""Reply-thread PDF attachments: REST refetch must resolve message references."""

from __future__ import annotations

import pytest

from oi_agent.config import Config, WatchTarget
from oi_agent.external.prepare import prepare_external_inputs
from oi_agent.external.records import STATUS_TEXT
from oi_agent.store import Store
from oi_agent.watch.attachments import context_attachments_requested, gather_candidates
from oi_agent.watch.discord_client import OIWatcher


class _FakeReference:
    def __init__(self, message_id: int, channel_id: int | None = None) -> None:
        self.message_id = message_id
        self.channel_id = channel_id
        self.guild_id = None


class _FakeAttachment:
    def __init__(
        self,
        attachment_id: str,
        filename: str,
        size: int,
        content_type: str = "application/pdf",
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
        *,
        content: str = "",
        attachments: list[_FakeAttachment] | None = None,
        referenced_message: object | None = None,
        reference: _FakeReference | None = None,
    ) -> None:
        self.id = message_id
        self.content = content
        self.attachments = list(attachments or ())
        self.referenced_message = referenced_message
        self.reference = reference


class _FakeChannel:
    def __init__(self, channel_id: int, messages: dict[int, _FakeMessage]) -> None:
        self.id = channel_id
        self._messages = messages
        self._history = list(messages.values())

    async def fetch_message(self, message_id: int) -> _FakeMessage:
        try:
            return self._messages[int(message_id)]
        except KeyError as exc:
            raise KeyError(message_id) from exc


def _invoice_pdf() -> bytes:
    text = b"BT /F1 16 Tf 40 140 Td (Invoice total: 37.50 USD) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n")
    xref = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(xref).encode() + b"\n%%EOF\n"
    )
    return bytes(result)


def _watcher(tmp_path) -> OIWatcher:
    cfg = Config()
    cfg.external_inputs = "read-only"
    cfg.watches = [WatchTarget(1559632120727605360, str(tmp_path / "repo"), 99, 3)]
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


def _large_pdf_bytes(target_size: int = 936_548) -> bytes:
    base = _invoice_pdf()
    if len(base) >= target_size:
        return base[:target_size]
    pad = target_size - len(base)
    return base + b"\n" + b" " * max(pad - 1, 0)


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


@pytest.mark.asyncio
async def test_same_scope_replies_resolves_reference_without_embed(tmp_path) -> None:
    """REST-fetched replies carry reference only; parent PDF must still resolve."""
    channel_id = 1559632120727605360
    parent_id = 1559886881657851904
    reply_id = 1559889355647090719
    pdf_bytes = _large_pdf_bytes()
    parent = _FakeMessage(
        parent_id,
        attachments=[
            _FakeAttachment("att1", "US_D2C_batch_1.pdf", len(pdf_bytes)),
        ],
    )
    reply = _FakeMessage(
        reply_id,
        content="@ultron !fresh read this us d2c batch 1 pdf as well",
        reference=_FakeReference(parent_id, channel_id),
        referenced_message=None,
    )
    channel = _FakeChannel(channel_id, {parent_id: parent, reply_id: reply})
    watcher = _watcher(tmp_path)

    resolved = await watcher._same_scope_replies(channel, [reply])

    assert len(resolved) == 1
    assert resolved[0].id == parent_id
    assert len(resolved[0].attachments) == 1


def test_reply_pdf_phrasing_does_not_require_history_opt_in() -> None:
    question = "read this us d2c batch 1 pdf as well and research on these companies"
    assert not context_attachments_requested(question)


@pytest.mark.asyncio
async def test_reply_pdf_prepares_through_gather_and_prepare(tmp_path) -> None:
    """~940 KiB PDF on the replied-to message becomes extracted evidence."""
    pdf_bytes = _large_pdf_bytes()
    parent_id = 1559886881657851904
    reply_id = 1559889355647090719
    parent = _FakeMessage(
        parent_id,
        attachments=[
            _FakeAttachment("att1", "US_D2C_batch_1.pdf", len(pdf_bytes)),
        ],
    )
    reply = _FakeMessage(
        reply_id,
        content="@ultron read this us d2c batch 1 pdf",
        reference=_FakeReference(parent_id, 1559632120727605360),
    )
    channel_id = 1559632120727605360
    channel = _FakeChannel(channel_id, {parent_id: parent, reply_id: reply})
    watcher = _watcher(tmp_path)
    reply_messages = await watcher._same_scope_replies(channel, [reply])
    candidates = gather_candidates(
        burst_messages=[reply],
        context_messages=[],
        reply_messages=reply_messages,
    )
    assert len(candidates) == 1

    prepared = await prepare_external_inputs(
        "read this us d2c batch 1 pdf",
        [_candidate_with_fetch(candidates[0], pdf_bytes)],
    )
    assert len(prepared.inputs.attachments) == 1
    record = prepared.inputs.attachments[0]
    assert record.status == STATUS_TEXT
    assert prepared.inputs.evidence
    assert "Invoice" in prepared.inputs.evidence[0].text


@pytest.mark.asyncio
async def test_prepare_external_includes_reply_attachment_not_history_noise(tmp_path) -> None:
    """History PDFs stay opt-in; same-scope reply PDFs are always candidates."""
    pdf_bytes = _invoice_pdf()
    parent = _FakeMessage(
        10,
        attachments=[_FakeAttachment("a", "deck.pdf", len(pdf_bytes))],
    )
    reply = _FakeMessage(
        11,
        content="read this us d2c batch 1 pdf",
        reference=_FakeReference(10, 111),
    )
    channel = _FakeChannel(111, {10: parent, 11: reply})
    watcher = _watcher(tmp_path)
    reply_messages = await watcher._same_scope_replies(channel, [reply])
    history_noise = _FakeMessage(
        99,
        attachments=[_FakeAttachment("x", "noise.pdf", 10)],
    )

    prepared = await watcher._prepare_external(
        "read this us d2c batch 1 pdf",
        burst_messages=[reply],
        context_messages=[history_noise],
        reply_messages=reply_messages,
        extraction_source="read this us d2c batch 1 pdf",
    )

    assert len(prepared.inputs.attachments) == 1
    assert prepared.inputs.attachments[0].filename == "deck.pdf"
