"""Two-run web research: a tool-free decision, then an isolated search run.

The answer session never receives web tools. A separate OpenCode run, in an
empty directory and an isolated home, is the only place ``websearch`` and
``webfetch`` are allowed. This module owns the decision, the public-URL
allowlist, and the outbound link filter. It does not spawn OpenCode.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

from .intent import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_DISABLED,
    REFUSAL_PRIVATE,
    PublicRefusal,
    PublicRequest,
    extract_public_request,
    is_plain_web_search,
    is_private_referential,
    plain_web_search_refusal,
)
from .records import EvidenceItem, PreparedInputs

logger = logging.getLogger(__name__)

# Discord message flag: suppress link embeds (1 << 2).
SUPPRESS_EMBEDS = 4

# Explicit commands stay forced-yes. Natural language is a model decision.
_COMMAND_RE = re.compile(
    r"^\s*!(?:news|search|fetch|url|web)\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"(?i)(?:https?://|file:)[^\s<>\"')\]]+")
_LINK_REF_RE = re.compile(r"(?i)\b(?:links?|urls?)\b")
_TRAILING = ".,;:)]"


def references_links(text: str) -> bool:
    """Return True when the message itself names links or URLs."""
    return _LINK_REF_RE.search(text or "") is not None


def new_empty_research_dir() -> Path:
    """Return a fresh empty directory for one research run's working directory."""
    path = Path(tempfile.mkdtemp(prefix="oi-research-cwd-"))
    path.chmod(0o700)
    return path

CLASSIFIER_SYSTEM = (
    "You decide whether a Discord request needs public web research and you "
    "write the one search query. Read only the CURRENT MESSAGE and the "
    "RECENT MESSAGES below. Output one JSON object and nothing else, exactly "
    '{"research": true or false, "query": "short public query or empty", '
    '"inferred": true or false}. '
    "Decide research from the CURRENT MESSAGE only: true when it asks to "
    "search, research, look up, or find public internet information (clients, "
    "news, products, pages, current facts outside the repository), or when it "
    "is an explicit !search/!news/!web command. research=false for repository, "
    "code, invoice, or file questions. "
    "Write query from the CURRENT MESSAGE when it names a topic; otherwise "
    "infer ONE short public search query from the RECENT MESSAGES, giving the "
    "most recent messages the most weight. The query is a public web search "
    "query in your own words: never quoted chat, no person names, no URLs, "
    "no private details. Use an empty query when nothing searchable can be "
    "inferred, and set inferred=true exactly when the query came from the "
    "RECENT MESSAGES instead of the CURRENT MESSAGE. "
    'Example: "search for possible clients for contractor portal NRW region" '
    '-> {"research": true, "query": "possible clients for contractor portal '
    'NRW region", "inferred": false}. '
    'Example: CURRENT MESSAGE "can you research please" with RECENT MESSAGES '
    "about finding ICP leads among D2C brands using Gorgias -> "
    '{"research": true, "query": "ICP leads D2C brands using Gorgias", '
    '"inferred": true}. '
    'Example: "what does invoice_service do" -> '
    '{"research": false, "query": "", "inferred": false}. '
    "Never invent a query that nothing in the request or recent messages supports."
)

FORMAT_REMINDER = (
    "Your previous reply omitted the required final-answer marker. "
    "Reply again with exactly one [[OI_FINAL_ANSWER]] marker, then the "
    "user-facing answer and nothing before the marker."
)

# Appended after the user message for the classifier's single retry when the
# first reply carried no usable JSON decision (narration, a denied-tool
# step-limit message, or nothing at all).
CLASSIFIER_RETRY_REMINDER = (
    "Your previous reply was not the required JSON object. Reply again with "
    "exactly one JSON object and nothing else: "
    '{"research": true or false, "query": "short public query or empty"}. '
    "No narration, no tool attempts, no markdown."
)

Complete = Callable[[str], Awaitable[str | None]]


@dataclass(frozen=True)
class ResearchDecision:
    """Whether the current request needs a research run."""

    research: bool
    query: str
    forced: bool = False
    refusal: str | None = None
    classifier_failure: bool = False
    inferred: bool = False


@dataclass(frozen=True)
class ResearchResult:
    """Bounded output of one isolated research run."""

    ok: bool
    summary: str = ""
    sources: tuple[str, ...] = ()
    category: str | None = None


async def command_decision(
    message: str,
    *,
    enabled: bool,
    complete: Complete,
    recent_messages: Sequence[str] = (),
) -> ResearchDecision | None:
    """Resolve an explicit ``!search``/``!news``/``!fetch``/``!web`` command.

    Returns None when the message is not a forced command. Disabled mode
    never researches and does not call a model. Empty and private-referential
    commands stay honest refusals, matching the existing limitation contract.
    A topic argument is decided by the classifier like any other ask: the
    classifier writes the short query (from the argument or, when the
    argument names none, from the recent messages). URL arguments and
    ``!fetch`` never call the classifier.
    """
    if not _COMMAND_RE.match(message or ""):
        return None
    if not enabled:
        return ResearchDecision(False, "", forced=True, refusal=REFUSAL_DISABLED)
    outcome = extract_public_request(message)
    if isinstance(outcome, PublicRefusal):
        return ResearchDecision(False, "", forced=True, refusal=outcome.reason)
    if not isinstance(outcome, PublicRequest) or not outcome.value.strip():
        return ResearchDecision(False, "", forced=True, refusal=REFUSAL_AMBIGUOUS)
    if outcome.kind == "url":
        return ResearchDecision(True, outcome.value.strip(), forced=True)
    argument = outcome.value.strip()
    return await _classify_request(
        argument,
        complete=complete,
        recent_messages=recent_messages,
        forced=True,
        fallback_query=argument,
    )


def _classifier_input(request: str, recent_messages: Sequence[str]) -> str:
    """Build the classifier prompt: current message plus labelled context.

    With no recent messages the input is the bare request, exactly as before
    context inference existed.
    """
    lines = [text.strip() for text in recent_messages if (text or "").strip()]
    if not lines:
        return request
    recent = "\n".join(f"- {line}" for line in lines)
    return (
        f"CURRENT MESSAGE:\n{request}\n\n"
        "RECENT MESSAGES (most recent last):\n"
        f"{recent}"
    )


async def _classify_request(
    request: str,
    *,
    complete: Complete,
    recent_messages: Sequence[str] = (),
    forced: bool,
    fallback_query: str | None = None,
) -> ResearchDecision:
    """Decide one ask with the tool-free classifier over request plus context.

    The classifier may read the recent channel messages to infer ONE short
    public search query in its own words; that query — never the raw chat —
    is the only text that can reach a research run. A private-referential
    query refuses. A forced ask without any usable query refuses honestly;
    a forced command falls back to its own cleaned argument only when the
    classifier itself fails.
    """
    prompt = _classifier_input(request, recent_messages)
    decision = parse_classifier_output(await complete(prompt))
    if decision.classifier_failure:
        decision = parse_classifier_output(
            await complete(f"{prompt}\n\n{CLASSIFIER_RETRY_REMINDER}")
        )
    if decision.classifier_failure:
        if fallback_query:
            return ResearchDecision(True, fallback_query, forced=True)
        return ResearchDecision(False, "", forced=forced, classifier_failure=True)
    if decision.research is not True:
        if forced:
            return ResearchDecision(False, "", forced=True, refusal=REFUSAL_AMBIGUOUS)
        return ResearchDecision(False, "")
    query = decision.query.strip()
    if not query:
        return ResearchDecision(False, "", forced=forced, refusal=REFUSAL_AMBIGUOUS)
    if is_private_referential(query):
        return ResearchDecision(False, "", forced=forced, refusal=REFUSAL_PRIVATE)
    return ResearchDecision(True, query, forced=forced, inferred=decision.inferred)


def _classifier_payload(raw: str | None) -> dict | None:
    """Return the classifier's JSON object, or None when the reply has none."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_classifier_output(raw: str | None) -> ResearchDecision:
    """Parse a tool-free classifier reply into a bounded decision.

    ``classifier_failure`` is True exactly when the reply carries no usable
    decision (empty, narration, or malformed JSON): the caller retries once
    with a strict-JSON reminder and otherwise fails the decision explicitly,
    never silently treating garbage as research=false.
    """
    payload = _classifier_payload(raw)
    if payload is None:
        return ResearchDecision(False, "", classifier_failure=True)
    if payload.get("research") is not True:
        return ResearchDecision(False, "")
    query = payload.get("query")
    if not isinstance(query, str):
        query = ""
    query = " ".join(query.split())[:200]
    return ResearchDecision(True, query, inferred=payload.get("inferred") is True)


def parse_research_output(raw: str | None, *, max_chars: int, max_sources: int) -> ResearchResult:
    """Parse a research-run reply into a bounded summary and source URLs."""
    if not raw or not raw.strip():
        return ResearchResult(False, category="empty_response")
    text = raw.strip()
    if "[[OI_FINAL_ANSWER]]" in text:
        tail = text.rsplit("[[OI_FINAL_ANSWER]]", 1)[1].strip()
        text = tail or text
    summary = ""
    sources: list[str] = []
    start = text.find("{")
    end = text.rfind("}")
    payload = None
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict):
        body = payload.get("summary")
        if isinstance(body, str):
            summary = body.strip()
        listed = payload.get("sources")
        if isinstance(listed, list):
            for item in listed:
                if isinstance(item, str) and is_public_http_url(item) and item not in sources:
                    sources.append(item)
    if not summary:
        summary = text
    for url in public_http_urls([text]):
        if url not in sources:
            sources.append(url)
    summary = summary[:max_chars]
    sources = sources[: max(1, max_sources)]
    if not summary and not sources:
        return ResearchResult(False, category="no_results")
    return ResearchResult(True, summary=summary, sources=tuple(sources))


async def decide_research(
    message: str,
    *,
    enabled: bool,
    complete: Complete,
    history_urls: Iterable[str] = (),
    recent_messages: Sequence[str] = (),
) -> ResearchDecision:
    """Decide research deterministically when possible, else one classifier call.

    ``external_inputs`` disabled never calls ``complete`` and never researches.
    URL commands never call it either. An explicit plain-language web-search
    request (``search the internet ...``, ``google ...``, ``look up ...
    online``) refuses honestly for a private-referential or repository-internal
    subject and otherwise goes through the classifier like any other ask. A
    message that refers to links (``link``/``links``/``url``/``urls``, any
    case, including German ``Link``/``Links``) with a non-empty public history
    is forced research on the message itself. The classifier answers everything
    else; it sees the current message plus the recent channel messages and
    writes the one short query, so the research run never receives raw chat.
    An unusable verdict is retried once with a strict-JSON reminder, and if it
    still fails the decision carries ``classifier_failure`` so the caller never
    silently answers without research.
    """
    decision = await _resolve_research(
        message,
        enabled=enabled,
        complete=complete,
        history_urls=history_urls,
        recent_messages=recent_messages,
    )
    # One INFO line per decision. Never the message text.
    logger.info(
        "[research] research decision research=%s forced=%s classifier_failure=%s "
        "query_chars=%d",
        decision.research,
        decision.forced,
        decision.classifier_failure,
        len(decision.query),
    )
    return decision


async def _resolve_research(
    message: str,
    *,
    enabled: bool,
    complete: Complete,
    history_urls: Iterable[str] = (),
    recent_messages: Sequence[str] = (),
) -> ResearchDecision:
    """Resolve the decision before the per-decision log line in decide_research."""
    forced = await command_decision(
        message,
        enabled=enabled,
        complete=complete,
        recent_messages=recent_messages,
    )
    if forced is not None:
        return forced
    if is_plain_web_search(message):
        if not enabled:
            return ResearchDecision(False, "", forced=True, refusal=REFUSAL_DISABLED)
        refusal = plain_web_search_refusal(message)
        if refusal is not None:
            return ResearchDecision(False, "", forced=True, refusal=refusal)
        return await _classify_request(
            (message or "").strip(),
            complete=complete,
            recent_messages=recent_messages,
            forced=True,
        )
    if not enabled:
        return ResearchDecision(False, "")
    if history_urls and _LINK_REF_RE.search(message or ""):
        return ResearchDecision(True, (message or "").strip(), forced=True)
    return await _classify_request(
        message or "",
        complete=complete,
        recent_messages=recent_messages,
        forced=False,
    )


def _host_blocked(host: str) -> bool:
    """Return True for hosts that are not public web content."""
    name = host.strip("[]").lower().rstrip(".")
    if not name or name == "localhost" or name.endswith(".localhost"):
        return True
    if name.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def is_public_http_url(url: str) -> bool:
    """Return True for a public http(s) URL safe to hand to a research run."""
    if not url or url.lower().startswith("file:"):
        return False
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if parts.username or parts.password:
        return False
    host = parts.hostname or ""
    if _host_blocked(host):
        return False
    return True


def public_http_urls(texts: Iterable[str]) -> list[str]:
    """Collect unique public http(s) URLs, dropping private and file URLs."""
    found: list[str] = []
    for text in texts:
        if not text:
            continue
        for raw in _URL_RE.findall(text):
            url = raw.rstrip(_TRAILING)
            if is_public_http_url(url) and url not in found:
                found.append(url)
    return found


def _normalized(url: str) -> str:
    return url.rstrip(_TRAILING)


def filter_outbound_urls(text: str, allowed: Iterable[str]) -> tuple[str, bool]:
    """Strip URLs that are not in the allowlist. Return text and link presence.

    The allowlist is the research run's source list plus URLs from the user's
    own message. A remaining http(s) URL means the poster should suppress embeds.
    """
    allowed_set = {_normalized(item) for item in allowed if item}
    def _replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        url = _normalized(raw)
        if url in allowed_set or any(url.startswith(item) or item.startswith(url) for item in allowed_set):
            return raw
        return "[link removed]"
    filtered = _URL_RE.sub(_replace, text or "")
    has_links = any(is_public_http_url(_normalized(item)) for item in _URL_RE.findall(filtered))
    return filtered, has_links


def fill_research_evidence(
    inputs: PreparedInputs,
    result: ResearchResult,
    *,
    max_sources: int,
) -> None:
    """Record the research summary as bounded untrusted evidence items."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sources = list(result.sources)[: max(1, max_sources)]
    summary = (result.summary or "").strip()
    before = len(inputs.evidence)
    if summary:
        inputs.evidence.append(EvidenceItem(
            kind="web",
            title="Web research summary",
            url=sources[0] if sources else "",
            source_name="research-run",
            retrieved_at=now,
            text=summary,
        ))
    for url in sources:
        host = (urlsplit(url).hostname or "source").lower()
        inputs.evidence.append(EvidenceItem(
            kind="web",
            title=host,
            url=url,
            source_name=host,
            retrieved_at=now,
            text="",
        ))
    # A summary row must not evict a source that already fit in max_sources,
    # and N requested links must survive as N source rows.
    new_items = inputs.evidence[before:]
    if len(inputs.evidence) > max_sources:
        allow = max(max_sources, len(new_items))
        prior_keep = max(0, allow - len(new_items))
        inputs.evidence[:] = [*inputs.evidence[:before][:prior_keep], *new_items]


def failure_text(error_class: str, *, code_question: bool = True) -> str:
    """Honest failure copy. Non-code questions must not claim a code check."""
    if code_question:
        return (
            f"I couldn't verify this against the code ({error_class}) "
            "and I'm not going to guess."
        )
    return (
        f"I couldn't produce a verified answer to that question ({error_class}). "
        "The reply did not follow the required format, so I'm not going to guess."
    )


def marker_retry_needed(*, error_class: str | None, had_content: bool) -> bool:
    """Return True when one format-reminder retry should run."""
    return error_class == "protocol_missing_final_marker" and had_content
