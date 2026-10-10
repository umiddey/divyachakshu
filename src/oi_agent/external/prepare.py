"""Turns an explicit public request and admitted attachments into bounded inputs.

This is the single place where a chat message becomes external evidence:

* It decides, from the CURRENT direct request only, whether a public lookup is
  warranted, and refuses referential/ambiguous asks honestly.
* It gathers attachments from every admitted burst source message and from
  same-scope referenced messages, deduplicating by attachment id and enforcing
  aggregate count and byte caps.
* It classifies each payload from its real bytes, extracts document text or
  validates images, and records honest status plus explicit limitations.

Raw bytes live only in the returned :class:`PreparedInputs`, which the caller
closes. Nothing here writes to OI SQLite or logs payload content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .documents import extract_document, sniff_kind
from .images import ImageInfo, ImagePreparation, inspect_image, prepare_images
from .intent import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_DISABLED,
    REFUSAL_PRIVATE,
    PublicRefusal,
    PublicRequest,
    extract_public_request,
)
from .records import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENTS,
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_TOTAL_ATTACHMENT_BYTES,
    STATUS_FAILED,
    STATUS_IMAGE,
    STATUS_LIMIT,
    STATUS_TEXT,
    STATUS_TOO_LARGE,
    STATUS_UNSUPPORTED,
    AttachmentInput,
    EvidenceItem,
    PreparedInputs,
    bounded_text,
    image_media_type_for,
)
from .retrieval import RetrievalError, fetch_public_document, search_news, search_web

logger = logging.getLogger(__name__)

# Hosts whose HTML is a news-aggregator wrapper rather than publisher content.
# An article excerpt is only ever attributed to a publisher URL that the
# fetch actually landed on.
_AGGREGATOR_HOSTS = frozenset({"news.google.com", "news.google.com."})


@dataclass(frozen=True)
class AttachmentCandidate:
    """One attachment the controller is authorized to consider.

    Produced by the platform adapter. Only the platform's own authorized
    scope may produce these, which is what keeps attachment fetching from
    becoming arbitrary user-supplied URL fetching.
    """

    attachment_id: str
    source_message_id: int
    filename: str
    declared_media_type: str
    declared_size: int
    fetch: object  # zero-arg async callable returning the real bytes


# Controller-note wording, per limitation category, for a public attempt that
# produced no evidence. When attachments are still served by an external run,
# this note is what reaches the prompt so the model reports the real
# capability gap instead of answering the public part from memory.
_LIMITATION_NOTES = {
    REFUSAL_DISABLED: (
        "public retrieval is disabled; this request was not answered from the internet"
    ),
    REFUSAL_AMBIGUOUS: (
        "ambiguous public request; no public topic or URL was named"
    ),
    REFUSAL_PRIVATE: (
        "private referential request; the public lookup was refused"
    ),
}


def limitation_note(category: str) -> str:
    """Return the bounded controller note for one limitation category."""
    return _LIMITATION_NOTES.get(category, f"retrieval unavailable: {category}")


@dataclass
class PreparedExternal:
    """Everything the native runner needs for one external-input run."""

    inputs: PreparedInputs = field(default_factory=PreparedInputs)
    request: PublicRequest | None = None
    refusal: str | None = None
    image_preparation: ImagePreparation | None = None
    retrieval_category: str | None = None

    @property
    def is_external_run(self) -> bool:
        """Return True when the run needs fresh, disposable, read-only state."""
        return self.inputs.has_external_content

    @property
    def limitation_category(self) -> str | None:
        """Bounded category when the public attempt produced no evidence.

        Non-``None`` exactly when the message explicitly attempted the public
        internet but this preparation carries no usable evidence: the gate is
        disabled, the ask is ambiguous or private-referential, or retrieval
        failed. Such a run must be answered with an honest limitation and must
        never fall back silently to a repository audit.
        """
        if self.refusal is not None:
            return self.refusal
        if self.request is not None and not self.inputs.evidence:
            return self.retrieval_category or "retrieval_unavailable"
        return None

    def close(self) -> None:
        """Delete every transient byte this preparation created."""
        self.inputs.close()


async def prepare_external_inputs(
    message: str,
    attachments: list[AttachmentCandidate] | None = None,
    *,
    retrieval_enabled: bool = True,
    evidence_items: int = MAX_EVIDENCE_ITEMS,
    max_attachments: int = MAX_ATTACHMENTS,
) -> PreparedExternal:
    """Prepare bounded external evidence and attachments for one run.

    Args:
        message: The current direct user message.
        attachments: Attachment candidates already authorized by the platform
            adapter, in admission order. EVERY candidate receives an honest
            record; none is silently dropped for being over a cap.
        retrieval_enabled: When False, no outbound request is made at all and
            the run reports an honest capability limitation instead.
        evidence_items: Maximum number of news/URL evidence items.
        max_attachments: Operator attachment-count limit (``oi config set
            max_attachments``). Candidates past the limit are reported as
            limit-reached, never pre-sliced away.

    Returns:
        A :class:`PreparedExternal`. Call :meth:`PreparedExternal.close` when
        the run finishes so no transient bytes survive.
    """
    prepared = PreparedExternal()
    if not retrieval_enabled:
        # A disabled gate makes ZERO outbound requests. If the message still
        # asks for a public lookup (usable request or refused attempt), say
        # so plainly instead of silently answering from repository knowledge.
        if extract_public_request(message) is not None:
            prepared.refusal = REFUSAL_DISABLED
    else:
        outcome = extract_public_request(message)
        if isinstance(outcome, PublicRequest):
            prepared.request = outcome
            await _collect_evidence(prepared, outcome, evidence_items)
        elif isinstance(outcome, PublicRefusal):
            prepared.refusal = outcome.reason
    if attachments:
        await _collect_attachments(prepared, attachments, max_attachments)
    category = prepared.limitation_category
    if category is not None and prepared.inputs.has_external_content:
        # The external run proceeds for the attachments, but the failed public
        # attempt must reach the prompt as an explicit controller note.
        prepared.inputs.notes.append(limitation_note(category))
    return prepared


async def _collect_evidence(
    prepared: PreparedExternal,
    request: PublicRequest,
    evidence_items: int,
) -> None:
    """Retrieve bounded public evidence for one explicit request."""
    limit = max(1, min(evidence_items, MAX_EVIDENCE_ITEMS))
    try:
        if request.kind == "news":
            headlines = await search_news(request.value, limit=limit)
            prepared.inputs.evidence.extend(headlines)
            await _attach_article_excerpt(prepared, headlines)
        elif request.kind == "web":
            prepared.inputs.evidence.extend(
                await search_web(request.value, limit=limit)
            )
        else:
            document = await fetch_public_document(request.value)
            _append_document_evidence(prepared, request, document.text)
    except RetrievalError as exc:
        # Honest, bounded limitation. Retrieval never degrades into a guess,
        # and the category is the only thing that reaches a log.
        logger.info(
            "[external] retrieval refused category=%s kind=%s", exc.category, request.kind
        )
        prepared.retrieval_category = exc.category
        prepared.inputs.notes.append(f"retrieval unavailable: {exc.category}")


async def _attach_article_excerpt(
    prepared: PreparedExternal,
    headlines: list[EvidenceItem],
) -> None:
    """Fetch bounded article text for the newest headline, when reachable.

    A headline is a citation, not evidence. When the publisher's page is
    publicly fetchable, the newest item is enriched with a clipped excerpt so
    the model can answer from content rather than from a title alone. Failure
    is not an error: the headline citation stands on its own and no partial
    body is ever invented. Only an ACTUAL PUBLISHER final URL may carry
    article text: if the fetch never leaves the news aggregator's wrapper
    (consent page, redirect loop back to the aggregator), the HTML that came
    back is aggregator content, not the publisher's article, and the item
    stays headline-only.
    """
    if not headlines:
        return
    newest = headlines[0]
    try:
        document = await fetch_public_document(newest.url)
    except (RetrievalError, OSError):
        return
    final_host = (urlsplit(document.final_url).hostname or "").lower()
    if final_host in _AGGREGATOR_HOSTS:
        return
    excerpt, truncated = bounded_text(document.text, MAX_EVIDENCE_CHARS)
    if not excerpt.strip():
        return
    prepared.inputs.evidence[0] = EvidenceItem(
        kind=newest.kind,
        title=newest.title,
        url=document.final_url,
        source_name=newest.source_name,
        retrieved_at=newest.retrieved_at,
        published_at=newest.published_at,
        text=excerpt,
        truncated=truncated,
        limitations=("article text clipped to a bounded excerpt",)
        if truncated else (),
    )


def _append_document_evidence(
    prepared: PreparedExternal,
    request: PublicRequest,
    text: str,
) -> None:
    """Record one public page as bounded, cited evidence."""
    excerpt, truncated = bounded_text(text, MAX_EVIDENCE_CHARS)
    if not excerpt.strip():
        prepared.inputs.notes.append("the requested page had no extractable text")
        return
    prepared.inputs.evidence.append(EvidenceItem(
        kind="web",
        title=f"public page {request.value}",
        url=request.value,
        source_name=_host_of(request.value),
        retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        text=excerpt,
        truncated=truncated,
        limitations=("page text clipped to a bounded excerpt",) if truncated else (),
    ))


def _host_of(url: str) -> str:
    """Return the bare host of a validated URL for citation display."""
    return (urlsplit(url).hostname or "unknown source").lower()


async def _collect_attachments(
    prepared: PreparedExternal,
    candidates: list[AttachmentCandidate],
    max_attachments: int = MAX_ATTACHMENTS,
) -> None:
    """Fetch, classify, and bound admitted attachments within aggregate caps.

    Every candidate gets an honest record, and every cap is enforced against
    the bytes that were ACTUALLY downloaded, not just the sizes the platform
    declared: a lying declared size cannot smuggle an oversized payload into
    the run, and overflow past any limit is reported, never pre-sliced away.
    """
    seen: set[str] = set()
    total_bytes = 0
    accepted = 0
    image_inputs: list[tuple[str, bytes, ImageInfo]] = []

    for candidate in candidates:
        if candidate.attachment_id in seen:
            continue
        seen.add(candidate.attachment_id)
        if accepted >= max_attachments:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=candidate.declared_size,
                kind="unknown",
                status=STATUS_LIMIT,
                limitations=(f"exceeded the {max_attachments}-attachment limit",),
            ))
            continue
        if candidate.declared_size > MAX_ATTACHMENT_BYTES:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=candidate.declared_size,
                kind="unknown",
                status=STATUS_TOO_LARGE,
                limitations=("declared size exceeds the per-attachment limit",),
            ))
            continue
        try:
            payload = await candidate.fetch()
        except Exception:  # noqa: BLE001 - adapter boundary; attachments are best-effort
            logger.info("[external] attachment fetch failed id_hash=%s", _stable_id(candidate.attachment_id))
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=candidate.declared_size,
                kind="unknown",
                status=STATUS_FAILED,
                limitations=("the attachment could not be downloaded",),
            ))
            continue
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=candidate.declared_size,
                kind="unknown",
                status=STATUS_FAILED,
                limitations=("the attachment was empty",),
            ))
            continue
        data = bytes(payload)
        if len(data) > MAX_ATTACHMENT_BYTES:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=len(data),
                kind="unknown",
                status=STATUS_TOO_LARGE,
                limitations=("downloaded size exceeds the per-attachment limit",),
            ))
            continue
        if total_bytes + len(data) > MAX_TOTAL_ATTACHMENT_BYTES:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=candidate.attachment_id,
                source_message_id=candidate.source_message_id,
                filename=candidate.filename,
                declared_media_type=candidate.declared_media_type,
                declared_size=len(data),
                kind="unknown",
                status=STATUS_TOO_LARGE,
                limitations=("would exceed the per-run aggregate attachment limit",),
            ))
            continue
        total_bytes += len(data)
        accepted += 1
        await _classify_attachment(prepared, candidate, data, image_inputs)

    if image_inputs:
        prepared.image_preparation = await prepare_images(
            image_inputs, prepared.inputs.workdir
        )
        for image in prepared.image_preparation.images:
            prepared.inputs.attachments.append(AttachmentInput(
                attachment_id=f"native-{image.label}",
                source_message_id=0,
                filename=image.path.name,
                declared_media_type=image.media_type,
                declared_size=image.path.stat().st_size if image.path.exists() else 0,
                kind="image",
                status=STATUS_IMAGE,
                limitations=(image.limitation,) if image.limitation else (),
            ))


def _stable_id(value: str) -> str:
    """Return a non-reversible, bounded token for log correlation."""
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


async def _classify_attachment(
    prepared: PreparedExternal,
    candidate: AttachmentCandidate,
    data: bytes,
    image_inputs: list[tuple[str, bytes, ImageInfo]],
) -> None:
    """Classify one real payload and record its honest bounded status."""
    label = f"message {candidate.source_message_id}: {candidate.filename}"
    info = await inspect_image(data)
    if info is not None:
        image_inputs.append((label, data, info))
        return
    if image_media_type_for("", data[:32]) is not None:
        # The bytes claim a supported image type but failed validation (past
        # the pixel/frame budget or malformed). Record that explicitly rather
        # than letting it fall through to text sniffing.
        prepared.inputs.attachments.append(AttachmentInput(
            attachment_id=candidate.attachment_id,
            source_message_id=candidate.source_message_id,
            filename=candidate.filename,
            declared_media_type=candidate.declared_media_type,
            declared_size=len(data),
            kind="image",
            status=STATUS_UNSUPPORTED,
            limitations=(
                "the image was not usable: it is malformed or past the "
                "pixel/frame budget",
            ),
        ))
        return

    kind = sniff_kind(candidate.filename, data)
    if kind == "unsupported":
        prepared.inputs.attachments.append(AttachmentInput(
            attachment_id=candidate.attachment_id,
            source_message_id=candidate.source_message_id,
            filename=candidate.filename,
            declared_media_type=candidate.declared_media_type,
            declared_size=len(data),
            kind="unsupported",
            status=STATUS_UNSUPPORTED,
            limitations=(
                "unsupported file type; only text, PDF, DOCX, PNG, JPEG, GIF, "
                "and WebP attachments are read",
            ),
        ))
        return

    result = await extract_document(candidate.filename, data)
    prepared.inputs.attachments.append(AttachmentInput(
        attachment_id=candidate.attachment_id,
        source_message_id=candidate.source_message_id,
        filename=candidate.filename,
        declared_media_type=candidate.declared_media_type,
        declared_size=len(data),
        kind="document",
        status=result.status,
        limitations=(result.limitation,) if result.limitation else (),
    ))
    if result.status == STATUS_TEXT and result.text:
        prepared.inputs.evidence.append(EvidenceItem(
            kind="document",
            title=candidate.filename,
            url="",
            source_name=f"attachment from message {candidate.source_message_id}",
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            text=result.text,
            truncated=result.truncated,
            limitations=(result.limitation,) if result.limitation else (),
        ))
