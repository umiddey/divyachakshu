"""Authorized same-scope attachment gathering for the Discord adapter.

This is the *platform adapter* side of the external-input boundary. Only the
platform's own authorized scope produces attachment candidates, which is what
keeps attachment fetching from becoming "fetch any URL a user typed":

* Candidates come from messages the watcher already admitted, from replies
  resolved inside the same authorized channel, and from the bounded history
  window that was actually rendered. Nothing else is ever fetched.
* The fetch target must be an exact approved Discord CDN host. A crafted
  attachment URL, a redirect, or a look-alike host is refused rather than
  fetched, and the connection is DNS-pinned to validated public addresses
  exactly like the public web path.
* Attachment URLs are signed and expiring. They are never logged, never put
  in a prompt, and never included in a reply. Only ids, filenames, declared
  types, and sizes are exposed.
* Attachment ids are deduplicated and aggregate count/byte caps are enforced
  by the caller, so a burst cannot multiply into an unbounded download.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..external.prepare import AttachmentCandidate
from ..external.retrieval import RetrievalError, _validate_url

logger = logging.getLogger(__name__)

# Historical-context attachment download is OPT-IN: history-window files are
# considered only when the CURRENT request explicitly refers to them. A bare
# attachment noun is not enough ("summarize report.pdf" attaches the burst
# file); the ask must place the subject in the conversation ("the file
# above", "that screenshot", "the documents posted earlier").
_ATTACHMENT_NOUN_RE = re.compile(
    r"\b(?:attachments?|files?|documents?|images?|screenshots?|pdfs?|"
    r"docx|uploads?|pictures?)\b",
    re.IGNORECASE,
)
_CONTEXT_REFERENCE_RE = re.compile(
    r"\b(?:above|aforementioned|earlier|previous|prior|before|history|"
    r"back then|posted before|sent before|shared earlier|uploaded before|"
    r"in the (?:thread|conversation|channel|history))\b"
    r"|\b(?:that|this|those|these|the)\s+(?:attachment|file|document|image|"
    r"screenshot|pdf|docx|upload|picture)s?\b",
    re.IGNORECASE,
)


def context_attachments_requested(question: str) -> bool:
    """Return True when the request explicitly asks about history files.

    Args:
        question: The current direct user request.

    Returns:
        True only when an attachment noun AND a conversation-relative
        reference both appear, so bounded history attachments are fetched on
        request instead of by default.
    """
    if not isinstance(question, str) or not question.strip():
        return False
    return bool(
        _ATTACHMENT_NOUN_RE.search(question)
        and _CONTEXT_REFERENCE_RE.search(question)
    )


AUTHOR_FOLLOW_UP_WINDOW_MINUTES = 15
AUTHOR_FOLLOW_UP_MAX_MESSAGES = 3


def _message_author_id(message: Any) -> int:
    return int(getattr(getattr(message, "author", None), "id", 0) or 0)


def _message_has_attachments(message: Any) -> bool:
    return bool(getattr(message, "attachments", None))


def _mentions_bot(message: Any, bot_user_id: int) -> bool:
    if not bot_user_id:
        return False
    for member in getattr(message, "mentions", None) or ():
        if int(getattr(member, "id", 0) or 0) == bot_user_id:
            return True
    return False


def author_follow_up_attachment_messages(
    *,
    burst_messages: list[Any],
    context_messages: list[Any],
    reply_messages: list[Any] | None = None,
    bot_user_id: int = 0,
    window_minutes: int = AUTHOR_FOLLOW_UP_WINDOW_MINUTES,
    max_prior_messages: int = AUTHOR_FOLLOW_UP_MAX_MESSAGES,
) -> list[Any]:
    """Return same-author history messages whose attachments follow a bare mention.

    When the trigger is a bot mention with no attachment and no reply thread,
    the author's own recent attachment message(s) in the rendered history
    window are eligible. Other authors' files are never pulled here.
    """
    if reply_messages:
        return []
    if not burst_messages or not bot_user_id:
        return []
    trigger = burst_messages[0]
    if not _mentions_bot(trigger, bot_user_id):
        return []
    if _message_has_attachments(trigger):
        return []
    author_id = _message_author_id(trigger)
    if not author_id:
        return []
    burst_ids = {int(getattr(message, "id", 0) or 0) for message in burst_messages}
    trigger_time = getattr(trigger, "created_at", None)
    if trigger_time is not None and trigger_time.tzinfo is None:
        trigger_time = trigger_time.replace(tzinfo=timezone.utc)
    window = timedelta(minutes=max(1, window_minutes))
    picked: list[Any] = []
    for message in reversed(context_messages or ()):
        mid = int(getattr(message, "id", 0) or 0)
        if not mid or mid in burst_ids:
            continue
        if _message_author_id(message) != author_id:
            continue
        if not _message_has_attachments(message):
            continue
        created = getattr(message, "created_at", None)
        if trigger_time is not None and isinstance(created, datetime):
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = trigger_time - created
            if age < timedelta(0) or age > window:
                continue
        picked.append(message)
        if len(picked) >= max(1, max_prior_messages):
            break
    picked.reverse()
    return picked


def history_messages_for_attachments(
    *,
    question: str,
    burst_messages: list[Any],
    context_messages: list[Any],
    reply_messages: list[Any] | None = None,
    bot_user_id: int = 0,
) -> list[Any]:
    """Choose which rendered-history messages may contribute attachment candidates."""
    if context_attachments_requested(question):
        return list(context_messages or ())
    follow_up = author_follow_up_attachment_messages(
        burst_messages=burst_messages,
        context_messages=context_messages,
        reply_messages=reply_messages,
        bot_user_id=bot_user_id,
    )
    return follow_up


def unloaded_history_attachment_note(
    *,
    context_messages: list[Any],
    burst_messages: list[Any],
    loaded_source_message_ids: set[int],
    bot_user_id: int = 0,
) -> str | None:
    """Return a controller note when history shows attachments the run did not load."""
    if not context_messages or not burst_messages:
        return None
    trigger = burst_messages[0]
    author_id = _message_author_id(trigger)
    if not author_id or not _mentions_bot(trigger, bot_user_id):
        return None
    missing: list[str] = []
    burst_ids = {int(getattr(message, "id", 0) or 0) for message in burst_messages}
    for message in context_messages:
        mid = int(getattr(message, "id", 0) or 0)
        if not mid or mid in burst_ids:
            continue
        if _message_author_id(message) != author_id:
            continue
        if not _message_has_attachments(message):
            continue
        if mid in loaded_source_message_ids:
            continue
        for attachment in getattr(message, "attachments", None) or ():
            name = str(getattr(attachment, "filename", "unnamed") or "unnamed")
            missing.append(name)
    if not missing:
        return None
    shown = ", ".join(sorted(set(missing))[:5])
    return (
        "thread history names attachment(s) the controller could not load for this "
        f"run ({shown}); describe that limitation honestly and never ask the user "
        "to save or copy files onto the host disk or repository paths"
    )


# Exact approved Discord CDN hosts. Anything else is refused before a socket
# is opened, so an attachment URL is never a general-purpose fetch primitive.
APPROVED_CDN_HOSTS = frozenset({
    "cdn.discordapp.com",
    "media.discordapp.net",
})

# One attachment download is bounded well below the aggregate cap so a single
# oversized file cannot consume the whole budget.
MAX_ATTACHMENT_FETCH_BYTES = 8 * 1024 * 1024
ATTACHMENT_TIMEOUT_SECONDS = 20.0


def is_approved_attachment_url(url: str) -> bool:
    """Return True only for an exact approved Discord CDN attachment URL.

    Args:
        url: Candidate attachment URL from the platform's own attachment
            object.

    Returns:
        True when the URL is an HTTPS URL (default port 443) on an approved
        CDN host. Plain HTTP and every non-CDN host are refused before a
        socket is opened.
    """
    if not isinstance(url, str) or not url:
        return False
    try:
        _validate_url(url)
    except RetrievalError:
        return False
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and host in APPROVED_CDN_HOSTS


@dataclass
class _AttachmentRecord:
    """Mutable per-run bookkeeping for one candidate attachment."""

    attachment_id: str
    source_message_id: int
    filename: str
    declared_media_type: str
    declared_size: int
    url: str


def _candidates_from_message(message: Any) -> list[_AttachmentRecord]:
    """Collect attachment records from one already-authorized message."""
    out: list[_AttachmentRecord] = []
    message_id = int(getattr(message, "id", 0) or 0)
    for attachment in getattr(message, "attachments", []) or ():
        url = str(getattr(attachment, "url", "") or "")
        # The Discord attachment id is globally unique: the same blob quoted
        # in a burst source and again in the history window is ONE
        # attachment, so the id alone (not the message id) is the dedupe key.
        attachment_id = str(
            getattr(attachment, "id", 0) or _stable_attachment_key(attachment)
        )[:64]
        if not is_approved_attachment_url(url):
            # A non-approved or malformed URL is not fetched at all. It is
            # still reported as metadata so the model is told the file exists
            # but could not be read.
            out.append(_AttachmentRecord(
                attachment_id=attachment_id,
                source_message_id=message_id,
                filename=str(getattr(attachment, "filename", "unnamed") or "unnamed"),
                declared_media_type=str(getattr(attachment, "content_type", "") or ""),
                declared_size=int(getattr(attachment, "size", 0) or 0),
                url="",
            ))
            continue
        out.append(_AttachmentRecord(
            attachment_id=attachment_id,
            source_message_id=message_id,
            filename=str(getattr(attachment, "filename", "unnamed") or "unnamed"),
            declared_media_type=str(getattr(attachment, "content_type", "") or ""),
            declared_size=int(getattr(attachment, "size", 0) or 0),
            url=url,
        ))
    return out


def _stable_attachment_key(attachment: Any) -> str:
    """Return a stable, bounded dedupe key for one attachment object."""
    key = getattr(attachment, "id", 0) or getattr(attachment, "filename", "") or "unknown"
    return str(key)[:64]


def gather_candidates(
    *,
    burst_messages: list[Any],
    context_messages: list[Any],
    reply_messages: list[Any] | None = None,
) -> list[AttachmentCandidate]:
    """Build deduplicated attachment candidates from authorized messages only.

    Sources, in priority order: every admitted burst source message (so an
    attachment on an earlier message of the burst is not lost just because the
    latest message has none), then same-scope referenced replies, then the
    bounded history window that was actually rendered.

    Args:
        burst_messages: The admitted source messages of this burst.
        context_messages: The messages that made up the rendered history.
        reply_messages: Referenced/replied-to messages in the same scope.

    Returns:
        Deduplicated candidates in priority order. Records with a refused
        URL are still returned so the caller can report an honest status.
    """
    ordered: list[Any] = []
    seen_messages: set[int] = set()
    for group in (burst_messages, reply_messages or [], context_messages):
        for message in group or ():
            mid = int(getattr(message, "id", 0) or 0)
            if mid in seen_messages:
                continue
            seen_messages.add(mid)
            ordered.append(message)

    deduped: list[_AttachmentRecord] = []
    seen_ids: set[str] = set()
    for message in ordered:
        for record in _candidates_from_message(message):
            if record.attachment_id in seen_ids:
                continue
            seen_ids.add(record.attachment_id)
            deduped.append(record)

    return [
        AttachmentCandidate(
            attachment_id=record.attachment_id,
            source_message_id=record.source_message_id,
            filename=record.filename,
            declared_media_type=record.declared_media_type,
            declared_size=record.declared_size,
            fetch=_make_fetcher(record),
        )
        for record in deduped
    ]


def _make_fetcher(record: _AttachmentRecord) -> Callable[[], Awaitable[bytes]]:
    """Return a zero-arg fetcher bound to one validated attachment record."""

    async def _fetch() -> bytes:
        if not record.url:
            raise RetrievalError("attachment_url_not_approved")
        return await fetch_attachment_bytes(record.url)

    return _fetch


async def fetch_attachment_bytes(url: str) -> bytes:
    """Fetch one approved attachment with pinned public destinations.

    Redirects are not followed: a signed CDN URL that redirects somewhere
    else is refused rather than chased. The response is size-bounded, and only
    the raw bytes are returned, never the URL.
    """
    from ..external.retrieval import USER_AGENT, _session

    async with _session() as session:
        try:
            async with session.get(
                url,
                allow_redirects=False,
                headers={"User-Agent": USER_AGENT},
            ) as response:
                if response.status != 200:
                    raise RetrievalError(f"attachment_http_{response.status // 100}xx")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if int(declared) > MAX_ATTACHMENT_FETCH_BYTES:
                            raise RetrievalError("attachment_too_large")
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_ATTACHMENT_FETCH_BYTES:
                        raise RetrievalError("attachment_too_large")
                return bytes(body)
        except RetrievalError:
            raise
        except (OSError, asyncio.TimeoutError) as exc:
            raise RetrievalError("attachment_transport_error") from exc
