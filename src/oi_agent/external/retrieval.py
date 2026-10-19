"""Controller-owned, DNS-pinned bounded retrieval of PUBLIC web content.

Security model (this module is the only outbound path OI has):

* The model never performs retrieval. It cannot choose a URL, follow a link,
  or issue a second request. Every request originates from the current direct
  public request in chat.
* Every connection destination is validated, not merely the hostname. All
  resolved addresses must be globally routable, and the validated addresses are
  pinned for the actual connection, so DNS cannot be re-resolved to a private
  address between check and connect (no resolve-then-fetch race).
* TLS hostname verification and SNI are preserved: the certificate is checked
  against the original hostname while the socket connects to the pinned IP.
* Loopback, RFC1918, link-local/cloud-metadata, carrier-grade NAT, multicast,
  reserved, unspecified, IPv4-mapped IPv6, and non-public IPv6 destinations are
  refused, as are URL userinfo, non-HTTP(S) schemes, unusual ports, and
  redirects that leave the validated public space.
* Responses are size-, time-, and content-type-bounded. Redirects are followed
  manually and re-validated at every hop, never delegated to an unpinned client.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, urlunsplit

import aiohttp
import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException
from yarl import URL

from .records import (
    MAX_EVIDENCE_CHARS,
    MAX_EVIDENCE_ITEMS,
    MAX_URL_BYTES,
    EvidenceItem,
    bounded_text,
)

# Public ports only. Everything else (SSH, SMB, alternate HTTP ports that
# commonly front internal services) is refused before any socket is opened.
ALLOWED_SCHEMES = ("https", "http")
ALLOWED_PORTS = (80, 443)

MAX_REDIRECTS = 3
CONNECT_TIMEOUT_SECONDS = 10.0
TOTAL_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = MAX_URL_BYTES
USER_AGENT = "divyachakshu/0.1 (+bounded public evidence retrieval)"

# Hostnames that never denote public internet content.
_BLOCKED_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".intranet", ".lan", ".home.arpa",
    ".corp", ".private", ".test", ".example", ".invalid", ".onion",
)
_BLOCKED_HOSTS = frozenset({
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "broadcasthost", "metadata", "metadata.google.internal",
})

# Only these response types are ever turned into evidence text.
_TEXTUAL_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/xml", "text/xml")

# Both endpoints are public, documented RSS interfaces that need no API key
# or account: the controller never holds a third-party search credential, and
# the model never chooses a query. News uses the Google News RSS vertical
# (publication timestamps and publisher names); ordinary web lookups use the
# general Bing RSS endpoint, which returns DIRECT result links, not a
# wrapper. A general web ask is NEVER silently downgraded to news, and a news
# ask is never silently upgraded.
_NEWS_ENDPOINT = "https://news.google.com/rss/search"
_WEB_ENDPOINT = "https://www.bing.com/search"

# Per-endpoint query parameters, merged into ONE urlencode call so the
# endpoint's own parameters (``format=rss`` for Bing) can never produce a
# malformed second ``?``. The user topic is always the ``q`` value.
_ENDPOINT_PARAMS = {
    "news": {"hl": "en-US", "gl": "US", "ceid": "US:en"},
    "web": {"format": "rss"},
}

class RetrievalError(RuntimeError):
    """Bounded, log-safe retrieval failure category.

    ``category`` is a fixed enum-like token. The underlying exception text,
    URL query strings, and response bodies never reach a log or a reply.
    """

    def __init__(self, category: str) -> None:
        """Store only the bounded failure category."""
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class FetchedDocument:
    """One bounded public document fetched by the controller."""

    url: str
    final_url: str
    content_type: str
    text: str
    truncated: bool
    raw: bytes = b""


class _PinnedResolver(aiohttp.abc.AbstractResolver):
    """Resolver that rejects non-public answers and pins the accepted ones.

    A host whose answer set contains ANY non-public address is refused
    outright rather than filtered: a mixed answer set is the classic DNS
    rebinding shape, where a public and a private address are handed out
    together so a later connection lands on the private one. Filtering would
    silently accept that host, so the whole set is rejected.
    aiohttp connects to the addresses returned here, so this is a real
    connection-destination guarantee, not a pre-flight probe. TLS still
    verifies the certificate against the requested hostname, because aiohttp
    keeps the original ``Host``/SNI while dialling the pinned address.
    """

    def __init__(self) -> None:
        """Wrap the default threaded resolver with address validation."""
        self._inner: aiohttp.ThreadedResolver = aiohttp.ThreadedResolver()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, object]]:
        """Resolve one host, refusing unless every answer is globally routable."""
        infos = await self._inner.resolve(host, port, family)
        if not infos:
            raise RetrievalError("dns_no_answer")
        for info in infos:
            address = info.get("host")
            if not isinstance(address, str) or not is_public_address(address):
                # Reject the whole set: a mixed public/private answer is a
                # rebinding attempt, not a partially usable host.
                raise RetrievalError("dns_answer_not_public")
        return list(infos)

    async def close(self) -> None:
        """Release the wrapped resolver."""
        await self._inner.close()


def is_public_address(address: str) -> bool:
    """Return True only for globally routable, non-special-purpose addresses.

    Refuses loopback, private, link-local (including the cloud metadata
    address), carrier-grade NAT, multicast, reserved, and unspecified ranges,
    for both families, after unwrapping IPv4-mapped IPv6 forms.
    """
    try:
        parsed = ipaddress.ip_address(address.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    sixtofour = getattr(parsed, "sixtofour", None)
    if sixtofour is not None:
        parsed = sixtofour
    if parsed.is_loopback or parsed.is_private or parsed.is_link_local:
        return False
    if parsed.is_multicast or parsed.is_reserved or parsed.is_unspecified:
        return False
    if not parsed.is_global:
        return False
    shared = getattr(parsed, "sixtofour", None)
    if shared is not None and not shared.is_global:
        return False
    teredo = getattr(parsed, "teredo", None)
    if teredo is not None and not (teredo[0].is_global and teredo[1].is_global):
        return False
    return True


# A netloc carrying brackets must be exactly a bracketed IPv6 literal with an
# optional port, so genuinely public IPv6 destinations validate while stray
# brackets (split-host confusion, userinfo smuggles) stay malformed.
_BRACKETED_NETLOC_RE = re.compile(r"^\[[0-9A-Fa-f:.]+\](?::\d+)?$")


def _validate_url(url: str) -> tuple[str, int, str]:
    """Validate scheme, userinfo, host, and port before any socket is opened.

    Returns:
        Tuple of (host, port, scheme) for the validated URL.

    Raises:
        RetrievalError: When the URL is not an acceptable public HTTP(S) URL.
    """
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise RetrievalError("invalid_url")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise RetrievalError("scheme_not_allowed")
    if parts.username or parts.password or "@" in parts.netloc:
        raise RetrievalError("userinfo_not_allowed")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise RetrievalError("invalid_url")
    netloc = parts.netloc
    if not netloc or any(ch in netloc for ch in "\\ \t"):
        raise RetrievalError("invalid_url")
    if ("[" in netloc or "]" in netloc) and not _BRACKETED_NETLOC_RE.fullmatch(netloc):
        # Brackets are only ever a whole IPv6 literal; anything else is a
        # malformed authority, not an exotic host. A validated literal then
        # meets the same public-address policy as every other destination.
        raise RetrievalError("invalid_url")
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_HOST_SUFFIXES):
        raise RetrievalError("host_not_public")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise RetrievalError("invalid_url") from exc
    if port not in ALLOWED_PORTS:
        raise RetrievalError("port_not_allowed")
    if _looks_like_ip_literal(host) and not is_public_address(host):
        raise RetrievalError("address_not_public")
    return host, port, parts.scheme.lower()


def _looks_like_ip_literal(host: str) -> bool:
    """Return True when the host is written as an IP address literal."""
    candidate = host.strip("[]")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def _normalize(url: str) -> str:
    """Strip fragments and normalize the scheme case without changing the host."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _session() -> aiohttp.ClientSession:
    """Build a session that never consults ambient proxy environment.

    ``trust_env=False`` is required: inheriting ``HTTP_PROXY``/``ALL_PROXY``
    from the host would move the real connection destination outside the
    validated public address set, and ``NO_PROXY`` handling would make the
    effective destination depend on host configuration rather than this policy.
    """
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=_PinnedResolver(),
            use_dns_cache=True,
            family=socket.AF_UNSPEC,
            ssl=True,
        ),
        trust_env=False,
        timeout=aiohttp.ClientTimeout(
            total=TOTAL_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
        ),
        auto_decompress=True,
        max_line_size=8192,
    )


async def _read_bounded(response: aiohttp.ClientResponse) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES`` and refuse anything larger."""
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_RESPONSE_BYTES:
                raise RetrievalError("response_too_large")
        except ValueError:
            pass
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RetrievalError("response_too_large")
    return bytes(body)


async def fetch_public_document(url: str) -> FetchedDocument:
    """Fetch one public URL with pinned destinations and manual redirects.

    Each redirect hop is re-validated against the same public-address policy,
    so a public host cannot bounce the controller into private space.

    Args:
        url: Absolute public URL supplied by the current direct user request.

    Returns:
        The bounded fetched document with its final URL.

    Raises:
        RetrievalError: For every refusal and transport failure category.
    """
    current = _normalize(url)
    seen: set[str] = set()
    async with _session() as session:
        for _hop in range(MAX_REDIRECTS + 1):
            host, _port, _scheme = _validate_url(current)
            if current in seen:
                raise RetrievalError("redirect_loop")
            seen.add(current)
            try:
                async with session.get(
                    current,
                    allow_redirects=False,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                        "Accept-Language": "en",
                    },
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location:
                            raise RetrievalError("redirect_without_location")
                        # Documented yarl API: resolve Location against the
                        # current URL, then stringify BEFORE normalizing.
                        # Passing the URL object to a str-based helper would
                        # fail on ``.strip``.
                        current = _normalize(str(URL(current).join(URL(location))))
                        _validate_url(current)
                        continue
                    if response.status != 200:
                        raise RetrievalError(f"http_status_{response.status // 100}xx")
                    content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    body = await _read_bounded(response)
            except RetrievalError:
                raise
            except aiohttp.ClientError as exc:
                raise RetrievalError("transport_error") from exc
            except (OSError, asyncio.TimeoutError) as exc:
                raise RetrievalError("transport_error") from exc
            if content_type and not any(
                content_type == allowed or content_type.endswith("+xml") and "xml" in allowed
                for allowed in _TEXTUAL_TYPES
            ):
                raise RetrievalError("content_type_not_allowed")
            # XML/RSS bodies go to the feed parser verbatim: stripping tags
            # first would destroy the document the parser must read. Only real
            # HTML is converted to readable text.
            decoded = body.decode("utf-8", errors="replace")
            if content_type in ("text/html", "application/xhtml+xml"):
                rendered = html_to_text(decoded)
            elif content_type == "text/plain":
                rendered = collapse_whitespace(decoded)
            else:
                rendered = decoded
            return FetchedDocument(
                url=_normalize(url),
                final_url=current,
                content_type=content_type or "text/plain",
                text=rendered,
                raw=body,
                truncated=len(body) >= MAX_RESPONSE_BYTES,
            )
    raise RetrievalError("too_many_redirects")


class _ReadableTextExtractor(HTMLParser):
    """Linear, case-preserving text extraction that skips noise elements.

    Script, style, head, and embedded-object content is dropped entirely so
    no remote resource reference or embedded script text can reach the model.
    No remote resource is ever fetched: this is pure local text extraction.
    Unlike the previous tag-stripping pass, element and text case are kept
    exactly as published, and the document is walked once.
    """

    _NOISE = frozenset({
        "script", "style", "head", "iframe", "object", "embed",
        "noscript", "svg", "template",
    })
    _BLOCK = frozenset({
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "blockquote", "pre", "table", "ul", "ol",
    })

    def __init__(self) -> None:
        """Start with no noise context and no collected text."""
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._noise_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        """Enter noise elements wholesale; block elements break lines."""
        if tag in self._NOISE:
            self._noise_depth += 1
        elif not self._noise_depth and tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Leave noise elements; block elements break lines."""
        if tag in self._NOISE:
            self._noise_depth = max(0, self._noise_depth - 1)
        elif not self._noise_depth and tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        """Keep text bytes verbatim outside noise elements."""
        if not self._noise_depth:
            self._chunks.append(data)

    def text(self) -> str:
        """Return the joined, whitespace-collapsed readable text."""
        return collapse_whitespace("".join(self._chunks))


def html_to_text(markup: str) -> str:
    """Extract bounded readable text from an HTML document.

    Malformed markup yields whatever readable text was extracted up to the
    malformed point; it never raises into the retrieval path.
    """
    extractor = _ReadableTextExtractor()
    try:
        extractor.feed(markup)
        extractor.close()
    except Exception:  # noqa: BLE001 - hostile markup must not break retrieval
        pass
    return extractor.text()


def collapse_whitespace(text: str) -> str:
    """Collapse runs of blank space while keeping paragraph breaks."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    kept = [line for line in lines if line]
    return "\n".join(kept)


# --- Public news and web retrieval ----------------------------------------------
#
# The controller never holds a third-party search credential, and the model
# never chooses a query; the endpoints and their per-kind parameters are
# defined with the module constants above.


def _rfc3339(value: str | None) -> str | None:
    """Parse an RSS publication date into a bounded ISO-8601 UTC stamp."""
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


# A DOCTYPE is the entry point for every entity-based XML attack (billion
# laughs, external entity fetch, quadratic blowup). The stdlib parser cannot
# disable entity resolution, and a byte-regex prefilter is encoding-dependent
# (UTF-16 bypassed the previous ASCII scan), so parsing uses the documented
# defusedxml API, which refuses DTDs, entity expansion, and external
# references regardless of the document encoding.
def parse_hardened_xml(payload: bytes) -> ET.Element:
    """Parse untrusted XML with explicit DTD/entity rejection.

    Args:
        payload: Raw, size-bounded XML bytes (any declared encoding).

    Returns:
        The parsed root element.

    Raises:
        RetrievalError: ``feed_declares_dtd`` when the document declares a
            DTD, declares entities, or references external resources;
            ``feed_unparseable`` when it is not well-formed XML.
    """
    try:
        return SafeET.fromstring(payload)
    except DefusedXmlException as exc:
        raise RetrievalError("feed_declares_dtd") from exc
    except ET.ParseError as exc:
        raise RetrievalError("feed_unparseable") from exc


async def _search_rss(query: str, endpoint: str, kind: str, limit: int) -> list[EvidenceItem]:
    """Run one bounded RSS search and parse it with hardened XML."""
    cleaned = " ".join(query.split())
    if not cleaned or len(cleaned) > 200:
        raise RetrievalError("invalid_query")
    params = {"q": cleaned, **_ENDPOINT_PARAMS[kind]}
    url = f"{endpoint}?{urlencode(params)}"
    _validate_url(url)
    document = await fetch_public_document(url)
    items = _parse_rss(document.raw or document.text.encode("utf-8"),
                       kind=kind, limit=limit,
                       retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not items:
        raise RetrievalError("no_results")
    return items


async def search_news(query: str, *, limit: int = 8) -> list[EvidenceItem]:
    """Retrieve fresh public news headlines for one explicit user query.

    Args:
        query: The public topic named in the current direct user request.
        limit: Maximum number of headlines to return.

    Returns:
        Bounded evidence items ordered as the feed published them.

    Raises:
        RetrievalError: When the query is unusable or retrieval is refused.
    """
    return await _search_rss(query, _NEWS_ENDPOINT, "news", limit)


async def search_web(query: str, *, limit: int = 8) -> list[EvidenceItem]:
    """Retrieve general public web results for one explicit user query.

    Args:
        query: The public subject named in the current direct user request.
        limit: Maximum number of results to return.

    Returns:
        Bounded evidence items. Web results carry no publication timestamp, so
        the limitation is recorded explicitly rather than implying recency.

    Raises:
        RetrievalError: When the query is unusable or retrieval is refused.
    """
    return await _search_rss(query, _WEB_ENDPOINT, "web", limit)


# One search-result description is bounded to an equal share of the total
# evidence budget, so no single result can crowd out the rest.
_RESULT_DESCRIPTION_CHARS = MAX_EVIDENCE_CHARS // MAX_EVIDENCE_ITEMS


def _parse_rss(payload: bytes, *, kind: str, limit: int, retrieved_at: str) -> list[EvidenceItem]:
    """Parse an RSS 2.0 feed into bounded, honestly limited evidence items.

    The WHOLE size-bounded feed is parsed first and only then are the
    newest ``limit`` items kept: publication times are sorted descending
    before the cap is applied, so a late feed order cannot hide fresh news
    behind stale items. Items without a publication time sort last and carry
    an explicit "time unknown" limitation instead of an implied one.
    """
    root = parse_hardened_xml(payload)
    parsed: list[EvidenceItem] = []
    for node in root.iter("item"):
        title = collapse_whitespace(unescape(_text_of(node, "title")))
        link = _text_of(node, "link").strip()
        published = _rfc3339(_text_of(node, "pubDate"))
        source_node = node.find("source")
        source_name = collapse_whitespace(
            unescape(source_node.text or "")) if source_node is not None else ""
        if not title or not link:
            continue
        try:
            _validate_url(link)
        except RetrievalError:
            continue
        if not source_name:
            # Honest provenance: a web search result names no publisher
            # element, so the direct result host is the source of record —
            # never the search engine that listed it.
            source_name = (urlsplit(link).hostname or "").lower() or "unknown publisher"
        notes = ["headline only; article body not fetched"]
        if not published:
            notes.append("publication time not reported by this source")
        text = ""
        if kind == "web":
            description = html_to_text(unescape(_text_of(node, "description")))
            text, clipped = bounded_text(description, _RESULT_DESCRIPTION_CHARS)
            if text and clipped:
                notes.append("result description clipped")
        parsed.append(EvidenceItem(
            kind=kind,
            title=title,
            url=link,
            source_name=source_name,
            retrieved_at=retrieved_at,
            published_at=published,
            text=text,
            limitations=tuple(notes),
        ))
    fresh = [item for item in parsed if item.published_at]
    undated = [item for item in parsed if not item.published_at]
    fresh.sort(key=lambda item: item.published_at, reverse=True)
    return (fresh + undated)[:limit]


def _text_of(node: ET.Element, tag: str) -> str:
    """Return the text of one child element, or an empty string."""
    child = node.find(tag)
    return (child.text or "") if child is not None else ""
