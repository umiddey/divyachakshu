"""Tests for deterministic, three-way public-request extraction.

Consumer-visible contract (correction 5):

* ``None`` — no public request at all: ordinary chat and generic repository
  asks are untouched and no outbound query is ever built from them.
* ``PublicRefusal`` — an explicit public attempt that cannot be served
  honestly (private referential subject, or intent without a subject).
* ``PublicRequest`` — a usable public topic or URL, including public
  technical topics whose names merely contain sensitive substrings.
"""

import asyncio

from oi_agent.external.intent import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_PRIVATE,
    PublicRefusal,
    PublicRequest,
    extract_public_request,
    is_plain_web_search,
    plain_web_search_refusal,
)
from oi_agent.external.research import command_decision


def _is_request(result) -> bool:
    return isinstance(result, PublicRequest)


def _is_refusal(result, reason: str) -> bool:
    return isinstance(result, PublicRefusal) and result.reason == reason


# --- no public request: ordinary behavior is untouched -----------------------


def test_ordinary_chat_names_no_public_request():
    assert extract_public_request("please review the watch module for races") is None


def test_generic_repository_search_sends_no_web_query():
    """A generic repository 'search for symbol' ask must not become a query."""
    assert extract_public_request("search for the parse_config function") is None
    assert extract_public_request("search for symbol") is None


def test_empty_message_is_no_request():
    assert extract_public_request("") is None
    assert extract_public_request("   ") is None


def test_private_referential_without_lookup_phrase_is_no_request():
    """Conversation talk with no lookup phrasing stays ordinary chat."""
    assert extract_public_request("the token above was rotated") is None


# --- usable public requests --------------------------------------------------


def test_natural_news_phrases_extract_topic():
    result = extract_public_request("get the latest news about Python releases")
    assert _is_request(result)
    assert result.kind == "news"
    assert result.value == "Python releases"


def test_web_search_phrase_extracts_web_kind():
    result = extract_public_request("search the web for Python 3.14 release notes")
    assert _is_request(result)
    assert result.kind == "web"
    assert result.value == "Python 3.14 release notes"


def test_latest_news_without_marker_after_topic_still_works():
    result = extract_public_request("what's the latest news about FastAPI")
    assert _is_request(result)
    assert result.kind == "news"
    assert result.value == "FastAPI"


def test_url_fetch_verb_extracts_url_request():
    result = extract_public_request("fetch https://example.com/blog/post one")
    assert _is_request(result)
    assert result.kind == "url"
    assert result.value == "https://example.com/blog/post"


def test_public_technical_topics_containing_sensitive_words_work():
    """Product names are public even though their names contain nouns."""
    for topic in ("AWS Secrets Manager", "JSON Web Tokens", "Visual Studio Code"):
        result = extract_public_request(f"search the web for {topic}")
        assert _is_request(result), topic
        assert result.value == topic
        command = extract_public_request(f"!search {topic}")
        assert _is_request(command), topic


# --- explicit attempts that must refuse honestly -----------------------------


def test_private_referential_ask_refuses_with_attempt():
    result = extract_public_request("search the web for the token above")
    assert _is_refusal(result, REFUSAL_PRIVATE)


def test_referential_context_refuses_even_without_determiner():
    result = extract_public_request("search the web for the error from the last message")
    assert _is_refusal(result, REFUSAL_PRIVATE)


def test_look_up_file_content_refuses():
    result = extract_public_request("look up what is in this file")
    assert _is_refusal(result, REFUSAL_PRIVATE)


def test_news_intent_without_subject_refuses_for_public_topic():
    result = extract_public_request("what's the latest news")
    assert _is_refusal(result, REFUSAL_AMBIGUOUS)


def test_private_referential_refuses_also_in_commands():
    result = extract_public_request("!search the token in the last message")
    assert _is_refusal(result, REFUSAL_PRIVATE)


def test_bare_command_without_topic_refuses_for_public_topic():
    assert _is_refusal(extract_public_request("!news"), REFUSAL_AMBIGUOUS)
    assert _is_refusal(extract_public_request("!search"), REFUSAL_AMBIGUOUS)


# --- commands ----------------------------------------------------------------


def test_news_command_maps_to_news_kind():
    result = extract_public_request("!news Python 3.14")
    assert _is_request(result)
    assert result.kind == "news"
    assert result.value == "Python 3.14"


def test_search_command_maps_to_web_kind():
    result = extract_public_request("!search Python 3.14 release notes")
    assert _is_request(result)
    assert result.kind == "web"


def test_fetch_command_maps_to_url_kind():
    result = extract_public_request("!fetch https://www.python.org/downloads/")
    assert _is_request(result)
    assert result.kind == "url"
    assert result.value == "https://www.python.org/downloads/"


def test_command_topics_are_cleaned_but_never_invented():
    result = extract_public_request("!news   Python 3.14 release notes ")
    assert _is_request(result)
    assert result.value == "Python 3.14 release notes"


def test_repository_internal_topic_never_becomes_query():
    assert extract_public_request("!search this repository") is not None
    result = extract_public_request("search the web for this repository's tests")
    assert not _is_request(result)


def test_multiline_search_command_extracts_topic():
    """A !search argument spanning lines still matches and cleans to one line."""
    result = extract_public_request(
        "!search mechanical parts suppliers\nfrom brand A and brand B"
    )
    assert _is_request(result)
    assert result.value == "mechanical parts suppliers from brand A and brand B"

    async def main() -> None:
        async def complete(text: str) -> str:
            return (
                '{"research": true, "query": "mechanical parts suppliers", '
                '"inferred": false}'
            )

        decision = await command_decision(
            "!search a\nb", enabled=True, complete=complete
        )
        assert decision is not None
        assert decision.research is True
        assert decision.forced is True

    asyncio.run(main())


# --- plain-language web-search trigger (deterministic, no classifier) --------


def test_plain_web_search_trigger_matches_explicit_asks():
    for text in (
        "Please search the internet and find us potential leads, "
        "target the retail sector in the US",
        "search the web for Python 3.14 release notes",
        "SEARCH ONLINE for retail leads",
        "google potential retail leads in the US",
        "look up mechanical parts suppliers online",
        "look it up online",
        "find us suppliers on the internet",
        "research the retail sector online",
    ):
        assert is_plain_web_search(text), text


def test_plain_web_search_trigger_ignores_ordinary_and_repository_asks():
    """No explicit public-web target word means the classifier still decides."""
    for text in (
        "please review the watch module for races",
        "search for the parse_config function",
        "search for symbol",
        "find the function that parses config",
        "what does invoice_service do",
        "google.com is down for me",
        "the google docs integration broke",
        "check the latest news about Python releases",
    ):
        assert not is_plain_web_search(text), text


def test_plain_web_search_refusal_for_private_referential_subject():
    assert (
        plain_web_search_refusal("search the web for the token above")
        == REFUSAL_PRIVATE
    )
    assert (
        plain_web_search_refusal("google the error from the last message")
        == REFUSAL_PRIVATE
    )
    assert plain_web_search_refusal("search the internet for retail leads") is None


def test_plain_web_search_refusal_for_repository_internal_subject():
    assert (
        plain_web_search_refusal("search the internet for this repo's tests")
        == REFUSAL_AMBIGUOUS
    )
    assert plain_web_search_refusal("research the code online") == REFUSAL_AMBIGUOUS
