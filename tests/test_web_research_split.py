"""Two-run web research: classifier, isolation, links, and marker repair."""

from __future__ import annotations

import asyncio
import json

import pytest

from oi_agent.config import Config
from oi_agent.external.prepare import PreparedExternal
from oi_agent.external.records import PreparedInputs
from oi_agent.external.research import (
    ResearchResult,
    decide_research,
    failure_text,
    fill_research_evidence,
    filter_outbound_urls,
    marker_retry_needed,
    new_empty_research_dir,
    parse_research_output,
    public_http_urls,
)
from oi_agent.opencode.native import DisposableNativeRoot
from oi_agent.opencode.policy import build_research_permission_policy
from oi_agent.opencode.prompt import FINAL_OUTPUT_MARKER
from oi_agent.opencode.runner import OpenCodeRunner
from oi_agent.store import Store
from oi_agent.watch.discord_client import OIWatcher


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


def _research_watcher(tmp_path, external_inputs="read-only"):
    """Build a watcher whose research path can be called directly."""
    cfg = Config()
    cfg.external_inputs = external_inputs
    cfg.max_external_evidence_items = 8
    return OIWatcher(cfg, Store(tmp_path / "state.db"))


def _install_research_spies(monkeypatch, watcher, *, classifier_payload):
    """Record classifier and research-run calls without spawning OpenCode."""
    classifier_calls: list[str] = []
    research_calls: list[dict] = []
    decisions: list = []

    async def classify(message: str) -> str:
        classifier_calls.append(message)
        return classifier_payload

    async def research(query, urls, *, max_sources, timeout=None):
        research_calls.append(
            {"query": query, "urls": list(urls), "max_sources": max_sources, "timeout": timeout}
        )
        return ResearchResult(True, summary="fetched", sources=tuple(urls))

    real_decide = decide_research

    async def spy_decide(*args, **kwargs):
        decision = await real_decide(*args, **kwargs)
        decisions.append(decision)
        return decision

    monkeypatch.setattr(watcher._runner, "classify_web_request", classify)
    monkeypatch.setattr(watcher._runner, "run_web_research", research)
    monkeypatch.setattr("oi_agent.watch.discord_client.decide_research", spy_decide)
    return classifier_calls, research_calls, decisions


@pytest.mark.asyncio
async def test_history_link_request_fetches_every_public_url_without_classifier(
    tmp_path, monkeypatch
):
    """Five public links in the window are all fetched; private URLs are not."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    history = [
        "https://example.com/a",
        "see https://example.com/b and http://10.0.0.1/x",
        "local http://localhost/status",
        "https://example.com/c",
        "no url here",
        "https://example.com/d",
        "https://example.com/e",
    ]
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "go through these links", history
    )
    assert researched is True
    assert classifier_calls == []
    assert decisions[0].forced is True
    assert decisions[0].research is True
    assert research_calls[0]["urls"] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
        "https://example.com/d",
        "https://example.com/e",
    ]
    assert research_calls[0]["max_sources"] >= 5
    assert list(sources) == research_calls[0]["urls"]
    assert "http://10.0.0.1/x" not in research_calls[0]["urls"]
    assert "http://localhost/status" not in research_calls[0]["urls"]


@pytest.mark.asyncio
async def test_two_history_links_pass_exactly_two_urls(tmp_path, monkeypatch):
    """Two public links become exactly two research URLs."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, _decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    prepared = PreparedExternal()
    await watcher._apply_research(
        prepared,
        "read the links",
        ["https://example.com/one", "https://example.org/two"],
    )
    assert research_calls[0]["urls"] == [
        "https://example.com/one",
        "https://example.org/two",
    ]
    assert research_calls[0]["max_sources"] >= 2


@pytest.mark.asyncio
async def test_link_words_without_urls_fall_back_to_classifier(tmp_path, monkeypatch):
    """No history URL means the classifier still decides; research is not forced."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "read the links", ["nothing to fetch here"]
    )
    assert classifier_calls == ["read the links"]
    assert decisions[0].forced is False
    assert decisions[0].research is False
    assert research_calls == []
    assert sources == ()
    assert researched is False


@pytest.mark.asyncio
async def test_disabled_external_inputs_never_research_history_links(tmp_path, monkeypatch):
    """Disabled mode does not classify or research, even when links are named."""
    watcher = _research_watcher(tmp_path, external_inputs="disabled")
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": true, "query": "links"}'
    )
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "go through these links", ["https://example.com/a"]
    )
    assert classifier_calls == []
    assert research_calls == []
    assert decisions[0].research is False
    assert sources == ()
    assert researched is False


@pytest.mark.asyncio
async def test_link_words_force_research_without_classifier():
    """link/url words plus history URLs skip the classifier, including German Link."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": false, "query": ""}'

    history = ["https://example.com/a"]
    for message in ("go through these links", "lies diese Links", "open the URL"):
        decision = await decide_research(
            message, enabled=True, complete=complete, history_urls=history
        )
        assert decision.research is True
        assert decision.forced is True
        assert decision.query == message
    assert calls == []
    plain = await decide_research(
        "what does invoice_service do",
        enabled=True,
        complete=complete,
        history_urls=history,
    )
    assert plain.research is False
    assert calls == ["what does invoice_service do"]


def test_parse_and_fill_keep_twelve_sources_at_max_sources_twelve():
    """Twelve sources stay twelve when the cap is twelve, summary included."""
    urls = [f"https://example.com/page/{index}" for index in range(12)]
    raw = json.dumps({"summary": "all twelve pages", "sources": urls})
    parsed = parse_research_output(raw, max_chars=6000, max_sources=12)
    assert list(parsed.sources) == urls
    inputs = PreparedInputs()
    fill_research_evidence(inputs, parsed, max_sources=12)
    kept = [item.url for item in inputs.evidence if item.text == ""]
    assert kept == urls


@pytest.mark.asyncio
async def test_research_budget_and_prompt_scale_with_every_listed_url(monkeypatch, tmp_path):
    """Steps and timeout grow with the URL list, and the prompt requires each fetch."""
    cfg = Config()
    cfg.opencode_steps = 4
    cfg.opencode_model = "provider/model"
    runner = OpenCodeRunner(cfg)
    seen: dict = {}

    monkeypatch.setattr(
        "oi_agent.opencode.runner.resolve_runtime_binary",
        lambda *_args, **_kwargs: "/tmp/opencode",
    )

    def fake_build(model, system, *, steps=8):
        seen["system"] = system
        seen["steps"] = steps
        return {"agent": "oi_research"}

    async def fake_init(*_args, **_kwargs):
        return tmp_path / "native.db"

    monkeypatch.setattr("oi_agent.opencode.runner.build_research_agent", fake_build)
    monkeypatch.setattr("oi_agent.opencode.runner.initialize_native_schema", fake_init)
    monkeypatch.setattr(
        "oi_agent.opencode.runner.copy_isolated_credentials", lambda *_a, **_k: 1
    )
    monkeypatch.setattr("oi_agent.opencode.runner.copy_model_catalog", lambda *_a, **_k: None)
    monkeypatch.setattr(
        "oi_agent.opencode.runner.DisposableNativeRoot",
        lambda: DisposableNativeRoot(),
    )

    async def fake_process(_self, _argv, prompt, _environment, _cwd, _disposable, *, timeout):
        seen["prompt"] = prompt
        seen["timeout"] = timeout
        return '{"summary":"ok","sources":["https://example.com"]}', None

    monkeypatch.setattr(OpenCodeRunner, "_research_process", fake_process)
    urls = ["https://example.com", "https://example.org", "https://example.net"]
    result = await runner.run_web_research("read these", urls, max_sources=3)
    assert result.ok is True
    assert seen["steps"] == 7
    assert seen["timeout"] == 180 + 60 * 3
    for text in (seen["system"], seen["prompt"]):
        assert "Webfetch every listed URL, one by one, then answer." in text
        assert "List each retrieved URL in sources." in text
        assert "not skipped silently." in text
    kept = await runner.run_web_research(
        "read these", urls, max_sources=3, timeout=90
    )
    assert kept.ok is True
    assert seen["timeout"] == 90
