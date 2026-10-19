"""Deterministic extraction of an EXPLICIT public request from the current message.

The only sanctioned source of an outbound query or URL is the current direct
user request. This module decides, without a model call, whether a message
carries a public retrieval request and what public topic or URL it names.

It fails closed and honestly, with a three-way answer:

* ``None`` — no public request at all: ordinary chat and repository asks
  (including generic "search for <symbol>" repository questions) proceed
  completely unchanged.
* :class:`PublicRefusal` — an explicit public attempt that cannot be served
  honestly: anaphoric/private referential asks ("the token above", "what is
  in this file", "the link I sent") or public intent without a nameable
  public subject. Callers answer with an honest limitation and never fall
  back silently to repository knowledge.
* :class:`PublicRequest` — a usable public topic or URL.

Only a topic or URL literally present in the current message becomes a
query. History, repository evidence, attachments, reflection output, and
existing sessions are never consulted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Explicit "look this up" phrasing. Each alternative is a PREFIX that ends
# where the public topic begins; the topic itself is captured once, after the
# alternation, so the whole pattern has exactly one named group.
_LOOKUP_PREFIXES = (
    r"latest news (?:about|on|for|regarding)",
    r"(?:news|headlines) (?:about|on|for|regarding)",
    r"search (?:the )?(?:web|internet|online|news) for",
    r"(?:web|internet|online) search for",
    r"look (?:this |it )?up on the (?:web|internet)",
    r"look up\b",
    r"find (?:me )?(?:the )?(?:latest news|news) (?:about|on|for)",
    r"search for",
    r"check (?:the )?(?:latest news|news|release notes|changelog) (?:about|on|for)",
)
_RETRIEVAL_RE = re.compile(
    r"\b(?:" + "|".join(_LOOKUP_PREFIXES) + r")\s*[:,\-]?\s*"
    r"(?P<topic>[^?!]+?)(?:\.(?:\s|$)|[?!]+\s*|$)",
    re.IGNORECASE,
)

# "What's the latest news / version / release about X" — the topic is optional
# here, so it needs its own pattern rather than a shared group.
_LATEST_RE = re.compile(
    r"\bwhat(?:'s| is) (?:the )?(?:latest|current|newest) "
    r"(?:news|version|release|status|changelog)"
    r"(?: (?:about|on|for|of)\s+(?P<topic>[^?!]+?)(?:\.(?:\s|$)|[?!]+\s*|$))?",
    re.IGNORECASE,
)

# Explicitly asking to read a specific public page.
_PAGE_RE = re.compile(
    r"\b(?:check|read|fetch|open|retrieve|summari[sz]e)\s+(?:me\s+)?(?:this|that)?\s*"
    r"(?:public\s+)?(?:web\s+)?(?:page|article|post|url|link|site)\s+"
    r"(?:at\s+|from\s+)?(?P<topic>[^?!]+?)(?:\.(?:\s|$)|[?!]+\s*|$)",
    re.IGNORECASE,
)

# A fetch verb applied directly to a URL supplied in this same message.
_FETCH_VERB_RE = re.compile(
    r"\b(?:fetch|open|read|summari[sz]e|check|look at)\b"
    r"|what(?:'s| is) (?:in|on)\b",
    re.IGNORECASE,
)

# Explicit command form: `!news <topic>` / `!search <topic>` / `!fetch <url>`.
# DOTALL: a multi-line argument ("!search suppliers\nfrom brand A") is one
# command, and _clean_topic collapses the argument to a single-line query.
_COMMAND_RE = re.compile(
    r"^\s*!(?P<command>news|search|fetch|url|web)\b(?P<argument>.*)$",
    re.IGNORECASE | re.DOTALL,
)

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)

# Plain-language requests that explicitly name the public web as the place to
# search ("search the internet and find us leads", "google X", "look up X
# online", "find/research X on the internet/online"). They force research
# without a classifier; the whole message (bot mention stripped) becomes the
# research brief. "google" matches only in verb position: no host
# ("google.com") and no product compound ("google docs", "google maps").
_PLAIN_WEB_SEARCH_RE = re.compile(
    r"\b(?:"
    r"search\s+(?:the\s+)?(?:web|internet)\b"
    r"|search\s+online\b"
    r"|google\s+(?!docs\b|maps\b|drive\b|meet\b|play\b|cloud\b|chrome\b|"
    r"scholar\b|translate\b|calendar\b|photos\b|sheets?\b|forms?\b|"
    r"analytics\b|workspace\b)\S"
    r"|look\s+(?:this\s+|it\s+|that\s+|them\s+)?up\b[^.?!]*\bonline\b"
    r"|find\b[^.?!]*\b(?:online|on\s+the\s+(?:internet|web))\b"
    r"|research\b[^.?!]*\b(?:online|on\s+the\s+(?:internet|web))\b"
    r")",
    re.IGNORECASE,
)

# Phrases that prove the ask is explicitly about the public internet.
_PUBLIC_MARKER_RE = re.compile(
    r"\b(?:web|internet|online|news|public|url|link|website|site|"
    r"headlines|release notes|changelog|documentation|docs)\b",
    re.IGNORECASE,
)

# Leading conversational framing that is not part of the topic.
_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(?:can you|could you|please|hey|hi|hello|oi|quick question)\b[,: ]*",
    re.IGNORECASE,
)

# Phrases that are never a public topic; they are repository-internal.
_INTERNAL_ONLY_RE = re.compile(
    r"\b(?:this repo(?:sitory)?|our repo(?:sitory)?|the code|the codebase|"
    r"the tests?|the source|our code)\b",
    re.IGNORECASE,
)

REFUSAL_PRIVATE = "private_referential_request"
REFUSAL_AMBIGUOUS = "ambiguous_public_request"
REFUSAL_DISABLED = "external_retrieval_disabled"

MAX_TOPIC_CHARS = 200


@dataclass(frozen=True)
class PublicRequest:
    """One explicit public retrieval request extracted from the current message."""

    kind: str
    value: str
    source: str

    def describe(self) -> str:
        """Render a bounded, log-safe description (no full query text)."""
        return f"{self.kind} source={self.source} value_length={len(self.value)}"


@dataclass(frozen=True)
class PublicRefusal:
    """An EXPLICIT public attempt that cannot be served honestly.

    ``reason`` is a bounded category token. The three-way contract is:

    * ``extract_public_request`` returns ``None`` — no public request at all;
      ordinary chat/repository behavior proceeds unchanged.
    * it returns a :class:`PublicRefusal` — the message explicitly attempted a
      public lookup (even when retrieval is disabled, failed, or the subject
      is private/ambiguous); the caller must answer with an honest limitation
      and must never fall back silently to repository knowledge.
    * it returns a :class:`PublicRequest` — a usable public topic or URL.
    """

    reason: str

    def describe(self) -> str:
        """Render the bounded refusal category."""
        return self.reason


# Sensitive subjects that are only private when referenced contextually (see
# :func:`is_private_referential`). Product names that merely CONTAIN these
# substrings — AWS Secrets Manager, JSON Web Tokens — stay public.
_SENSITIVE_NOUN_RE = re.compile(
    r"\b(?:tokens?|secrets?|passwords?|credentials?|api[- ]?keys?|"
    r"values?|numbers?|codes?|attachments?|files?|documents?|uploads?|"
    r"messages?|screenshots?|code ?blocks?|snippets?|excerpts?|diffs?|"
    r"patches?|errors?|tracebacks?|logs?)\b",
    re.IGNORECASE,
)

# A determiner immediately before the sensitive noun ("the token", "my
# password", "this file") marks conversation-relative reference. A product
# name never has a determiner directly before the noun ("AWS Secrets
# Manager", "JSON Web Tokens").
_DETERMINED_NOUN_RE = re.compile(
    r"\b(?:the|that|this|those|these|my|our|its|his|her|their)\s+"
    r"(?:tokens?|secrets?|passwords?|credentials?|api[- ]?keys?|"
    r"values?|numbers?|codes?|attachments?|files?|documents?|uploads?|"
    r"messages?|screenshots?|code ?blocks?|snippets?|excerpts?|diffs?|"
    r"patches?|errors?|tracebacks?|logs?)\b",
    re.IGNORECASE,
)

# Conversation-relative context anywhere in the ask ("the token above", "the
# error from the last message") makes the subject private even without a
# determiner.
_REFERENTIAL_CONTEXT_RE = re.compile(
    r"\b(?:above|aforementioned|below|earlier|previous|prior|last|latest|"
    r"sent|posted|shared|pasted|attached|uploaded|mentioned|quoted|"
    r"discussed)\b|"
    r"\bfrom\s+(?:the\s+|this\s+|that\s+|my\s+|our\s+)?"
    r"(?:message|conversation|chat|history|thread|file|document|log|diff|"
    r"attachment|paste)\b|"
    r"\bin\s+(?:the\s+|this\s+|that\s+|my\s+|our\s+)?"
    r"(?:message|conversation|chat|history|thread|file|document|log|diff|"
    r"attachment|paste)\b",
    re.IGNORECASE,
)


def is_private_referential(text: str) -> bool:
    """Return True when the subject references private conversation content.

    A sensitive noun alone is not enough: "AWS Secrets Manager" and "JSON Web
    Tokens" are public technical topics. The subject is private when a
    determiner sits directly before the sensitive noun, or when the ask
    positions the subject relative to the conversation ("above", "from the
    last message").
    """
    if not _SENSITIVE_NOUN_RE.search(text):
        return False
    return bool(_DETERMINED_NOUN_RE.search(text) or _REFERENTIAL_CONTEXT_RE.search(text))


def extract_public_request(message: str) -> PublicRequest | PublicRefusal | None:
    """Extract an explicit public request from the current message only.

    Args:
        message: The current direct user message, after mention resolution.

    Returns:
        ``None`` when the message carries no public request at all (ordinary
        chat; callers proceed unchanged), a :class:`PublicRefusal` when the
        message explicitly attempted a public lookup that cannot be served
        honestly (private/referential subject, or intent without a usable
        subject), and a :class:`PublicRequest` when a public topic or URL is
        usable. The function never invents a topic and never reads anything
        other than ``message``.

    Distinguishing "no request" from "refused attempt" is the contract: a
    refused attempt must surface as an honest limitation (never as a silent
    fallback to repository knowledge), while ordinary repository asks such as
    "search for the parse function" must keep flowing through the normal
    path untouched.
    """
    if not isinstance(message, str) or not message.strip():
        return None
    text = message.strip()

    command = _COMMAND_RE.match(text)
    if command:
        return _from_command(command.group("command").lower(), command.group("argument"))

    # A URL explicitly supplied in the current request is fetchable on its own,
    # provided the message actually asks for a fetch.
    url = _first_public_url(text)
    if url and _FETCH_VERB_RE.search(text):
        return PublicRequest(kind="url", value=url, source="explicit_url")

    topic, kind, source, attempted = _match_topic(text)
    if not topic:
        if url and _PUBLIC_MARKER_RE.search(text):
            return PublicRequest(kind="url", value=url, source="explicit_url")
        if attempted:
            # Public intent with no nameable subject ("what's the latest
            # news") is an honest refusal asking for a public topic.
            return PublicRefusal(REFUSAL_AMBIGUOUS)
        return None
    # A private referential subject cannot become an outbound query, but the
    # attempt itself must be answered honestly.
    if is_private_referential(topic) or is_private_referential(text):
        return PublicRefusal(REFUSAL_PRIVATE)
    # Public intent is required: a generic repository "search for <symbol>"
    # is ordinary chat, not a web query.
    if not (_PUBLIC_MARKER_RE.search(text) or _PUBLIC_MARKER_RE.search(topic)):
        return None
    return PublicRequest(kind=kind, value=topic, source=source)


def is_plain_web_search(message: str) -> bool:
    """Return True when the message explicitly asks for a public web search.

    The deterministic plain-language counterpart of the ``!search`` command:
    an explicit public-web target word (``internet``/``web``/``online``), the
    verb ``google``, or ``look up``/``find``/``research`` tied to such a word.
    Ordinary chat and repository asks (``search for the parse_config
    function``) never match, so the classifier keeps deciding those.
    """
    if not isinstance(message, str):
        return False
    return bool(_PLAIN_WEB_SEARCH_RE.search(message))


def plain_web_search_refusal(message: str) -> str | None:
    """Refusal category for an explicit plain-language web-search request
    that cannot be served honestly, or ``None`` when it is serviceable.

    Mirrors the ``!search`` command contract: a repository-internal subject is
    refused exactly as the command path refuses it today, and a
    private-referential subject never becomes an outbound query.
    """
    text = (message or "").strip()
    if _INTERNAL_ONLY_RE.search(text):
        return REFUSAL_AMBIGUOUS
    if is_private_referential(text):
        return REFUSAL_PRIVATE
    return None


def _from_command(name: str, argument: str) -> PublicRequest | PublicRefusal:
    """Resolve an explicit ``!news``/``!search``/``!fetch`` style command.

    Commands are always an explicit public attempt, so every non-usable
    outcome is a refusal, never ``None``. Private referential subjects refuse
    here too, while public technical topics pass unchanged.
    """
    cleaned = argument.strip()
    url = _first_public_url(cleaned)
    if url:
        return PublicRequest(kind="url", value=url, source=f"!{name}")
    if name in ("news", "search", "web"):
        if not cleaned:
            return PublicRefusal(REFUSAL_AMBIGUOUS)
        if is_private_referential(cleaned):
            return PublicRefusal(REFUSAL_PRIVATE)
        topic = _clean_topic(cleaned)
        if topic:
            kind = "news" if name == "news" else "web"
            return PublicRequest(kind=kind, value=topic, source=f"!{name}")
        return PublicRefusal(REFUSAL_AMBIGUOUS)
    return PublicRefusal(REFUSAL_AMBIGUOUS)


def _match_topic(text: str) -> tuple[str, str, str, bool]:
    """Return ``(topic, kind, source, attempted)`` for the first lookup match.

    ``attempted`` is True when a lookup phrase was present but produced no
    usable subject, which the caller reports as an honest refusal.
    """
    match = _RETRIEVAL_RE.search(text)
    if match is not None:
        topic = _clean_topic(match.group("topic"))
        if topic:
            kind = "news" if re.search(
                r"\bnews\b|\bheadlines\b", text, re.IGNORECASE
            ) else "web"
            return topic, kind, "explicit_phrase", False
        return "", "", "", True
    match = _PAGE_RE.search(text)
    if match is not None:
        topic = _clean_topic(match.group("topic"))
        # A "read this page" ask may be naming a bare URL as its topic.
        url = _first_public_url(match.group("topic"))
        if url:
            return url, "url", "explicit_page", False
        if topic:
            return topic, "web", "explicit_page", False
        return "", "", "", True
    match = _LATEST_RE.search(text)
    if match is not None:
        raw = match.group("topic")
        topic = _clean_topic(raw or "")
        if not topic:
            # "What's the latest news" with no topic names no public subject.
            # Inventing a default topic would fabricate a request.
            return "", "", "", True
        return topic, "news", "explicit_latest", False
    return "", "", "", False


def _first_public_url(text: str) -> str | None:
    """Return the first syntactically public http(s) URL in the text."""
    for candidate in _URL_RE.findall(text):
        cleaned = candidate.rstrip(".,;:!?)\"'")
        parts = urlsplit(cleaned)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            continue
        return cleaned
    return None


def _clean_topic(raw: str) -> str:
    """Normalize an extracted topic into a bounded single-line query.

    A topic that is only repository-internal language is not a public subject,
    so it never becomes an outbound query.
    """
    without_greeting = _QUESTION_PREFIX_RE.sub("", raw)
    topic = " ".join(without_greeting.split()).strip(" \t\"'`.,:;-")
    if not topic:
        return ""
    if _INTERNAL_ONLY_RE.search(topic):
        return ""
    if len(topic) > MAX_TOPIC_CHARS:
        topic = topic[:MAX_TOPIC_CHARS].rstrip()
    return topic
