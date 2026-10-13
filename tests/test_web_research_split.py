"""Two-run web research: classifier, isolation, links, and marker repair."""

from __future__ import annotations

import asyncio

import pytest

from oi_agent.external.research import (
    decide_research,
    failure_text,
    filter_outbound_urls,
    marker_retry_needed,
    new_empty_research_dir,
    public_http_urls,
)
from oi_agent.opencode.native import DisposableNativeRoot
from oi_agent.opencode.policy import build_research_permission_policy
from oi_agent.opencode.prompt import FINAL_OUTPUT_MARKER


@pytest.mark.asyncio
async def test_classifier_research_true_false_and_disabled_makes_no_call():
    """The two brief examples classify, and disabled mode does not call the model."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        if "invoice_service" in text:
            return '{"research": false, "query": ""}'
        if "possible clients" in text:
            return (
                '{"research": true, "query": '
                '"possible clients for contractor portal NRW region"}'
            )
        return '{"research": false, "query": ""}'

    yes = await decide_research(
        "search for possible clients for contractor portal NRW region",
        enabled=True,
        complete=complete,
    )
    assert yes.research is True
    assert "NRW" in yes.query
    no = await decide_research(
        "what does invoice_service do",
        enabled=True,
        complete=complete,
    )
    assert no.research is False
    assert calls == [
        "search for possible clients for contractor portal NRW region",
        "what does invoice_service do",
    ]
    calls.clear()
    disabled = await decide_research(
        "search for possible clients for contractor portal NRW region",
        enabled=False,
        complete=complete,
    )
    assert disabled.research is False
    assert calls == []


def test_research_policy_denies_everything_except_web_tools():
    """The research agent may websearch and webfetch, and nothing else."""
    rules = build_research_permission_policy()
    allowed = {rule["action"] for rule in rules if rule["effect"] == "allow"}
    assert allowed == {"websearch", "webfetch"}
    denied = {rule["action"] for rule in rules if rule["effect"] == "deny"}
    for action in ("*", "read", "glob", "edit", "shell", "external_directory"):
        assert action in denied


def test_research_cwd_is_empty_and_env_has_no_oi_secret_names(monkeypatch):
    """Research isolation is an empty directory and an env without OI secrets."""
    monkeypatch.setenv("OI_DISCORD_TOKEN", "present")
    monkeypatch.setenv("PATH", "/usr/bin")
    cwd = new_empty_research_dir()
    root = DisposableNativeRoot()
    try:
        assert cwd.is_dir()
        assert list(cwd.iterdir()) == []
        names = set(root.environment())
        assert "OI_DISCORD_TOKEN" not in names
        assert all(not name.startswith("OI_") for name in names)
        assert not any(
            term in name.upper()
            for name in names
            for term in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
        )
    finally:
        root.close()
        cwd.rmdir()


def test_earlier_public_links_reach_research_input_and_private_urls_drop():
    """Recent-channel public URLs are kept; private and file URLs are not."""
    current = "research X from the links posted earlier"
    history = [
        "see https://example.com/clients",
        "also http://127.0.0.1/admin and http://10.1.2.3/x",
        "lan http://192.168.1.9/secret file:///etc/passwd",
        "local http://localhost/status",
    ]
    urls = public_http_urls([current, *history])
    assert urls == ["https://example.com/clients"]


def test_outbound_filter_keeps_sources_and_user_urls_only():
    """Unknown URLs are removed; a source URL and the user's URL stay."""
    user = "look at https://example.com/mine"
    sources = ["https://publisher.example/nrw"]
    text = (
        "See https://publisher.example/nrw and https://example.com/mine "
        "but not https://evil.example/phish"
    )
    filtered, has_links = filter_outbound_urls(text, [*sources, *public_http_urls([user])])
    assert "https://publisher.example/nrw" in filtered
    assert "https://example.com/mine" in filtered
    assert "https://evil.example/phish" not in filtered
    assert "[link removed]" in filtered
    assert has_links is True


def test_missing_marker_retries_once_then_non_code_failure():
    """A contentful unmarked answer is repaired once; a second miss is not a code check."""
    assert marker_retry_needed(
        error_class="protocol_missing_final_marker", had_content=True
    )
    assert not marker_retry_needed(
        error_class="protocol_missing_final_marker", had_content=False
    )

    async def _once():
        calls: list[str] = []

        async def retry(reminder: str) -> str:
            calls.append(reminder)
            return f"{FINAL_OUTPUT_MARKER}\nclients in NRW"

        from oi_agent.opencode.prompt import FINAL_OUTPUT_MARKER as marker
        from oi_agent.opencode.runner import _extract_marked_answer

        first, err = _extract_marked_answer("a useful answer with no marker")
        assert err == "protocol_missing_final_marker"
        assert first == ""
        assert marker_retry_needed(error_class=err, had_content=True)
        repaired = await retry(
            "Reply again with exactly one [[OI_FINAL_ANSWER]] marker"
        )
        answer, err2 = _extract_marked_answer(repaired)
        assert err2 is None
        assert answer == "clients in NRW"
        assert len(calls) == 1
        still, err3 = _extract_marked_answer("still no marker")
        assert still == ""
        assert err3 == "protocol_missing_final_marker"
        text = failure_text(err3, code_question=False)
        assert "against the code" not in text
        assert "protocol_missing_final_marker" in text
        code = failure_text(err3, code_question=True)
        assert "against the code" in code
        return marker

    asyncio.run(_once())
