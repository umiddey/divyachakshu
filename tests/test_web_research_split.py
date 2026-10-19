"""Two-run web research: classifier, isolation, links, and marker repair."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from oi_agent.config import Config
from oi_agent.external.intent import (
    REFUSAL_AMBIGUOUS,
    REFUSAL_DISABLED,
    REFUSAL_PRIVATE,
)
from oi_agent.external.prepare import PreparedExternal
from oi_agent.external.records import PreparedInputs
from oi_agent.external.research import (
    CLASSIFIER_RETRY_REMINDER,
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
from oi_agent.opencode.runner import (
    RESEARCH_BASE_TIMEOUT_SECONDS,
    RESEARCH_PER_URL_TIMEOUT_SECONDS,
    OpenCodeRunner,
)
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
    assert len(classifier_calls) == 1
    assert "read the links" in classifier_calls[0]
    assert "nothing to fetch here" in classifier_calls[0]
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
    assert seen["timeout"] == RESEARCH_BASE_TIMEOUT_SECONDS + RESEARCH_PER_URL_TIMEOUT_SECONDS * 3
    assert seen["timeout"] > 180 + 60 * 3
    for text in (seen["system"], seen["prompt"]):
        assert "Webfetch every listed URL, one by one, then answer." in text
        assert "List each retrieved URL in sources." in text
        assert "not skipped silently." in text
    kept = await runner.run_web_research(
        "read these", urls, max_sources=3, timeout=90
    )
    assert kept.ok is True
    assert seen["timeout"] == 90


_CLASSIFIER_NARRATION = (
    "**Maximum steps for this agent have been reached.** Tools are disabled "
    "until the next user input, so I am responding with a text-only summary. "
    "No web research was actually conducted."
)


@pytest.mark.asyncio
async def test_unusable_classifier_narration_becomes_honest_limitation(
    tmp_path, monkeypatch, caplog
):
    """Narration instead of JSON retries once, then fails honestly, never silently."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload=_CLASSIFIER_NARRATION
    )
    prepared = PreparedExternal()
    with caplog.at_level(logging.INFO):
        sources, researched = await watcher._apply_research(
            prepared,
            "Please find us potential leads, target the retail sector in the US",
            [],
        )
    assert classifier_calls == [
        "Please find us potential leads, target the retail sector in the US",
        "Please find us potential leads, target the retail sector in the US\n\n"
        + CLASSIFIER_RETRY_REMINDER,
    ]
    assert research_calls == []
    assert researched is False
    assert sources == ()
    assert prepared.research_failure == "classifier_unavailable"
    assert decisions[0].research is False
    assert decisions[0].classifier_failure is True
    decision_lines = [
        record.getMessage()
        for record in caplog.records
        if "research decision" in record.getMessage()
    ]
    assert decision_lines, "the decision must be logged"
    assert "research=False" in decision_lines[0]
    assert "classifier_failure=True" in decision_lines[0]
    assert "potential leads" not in decision_lines[0]


@pytest.mark.asyncio
async def test_research_run_logs_only_bounded_counts(tmp_path, monkeypatch, caplog):
    """The run log carries counts only — never query or URL text."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, _decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload='{"research": true, "query": "retail leads NRW"}',
    )
    prepared = PreparedExternal()
    with caplog.at_level(logging.INFO):
        await watcher._apply_research(
            prepared,
            "please research retail leads from https://example.com/page",
            ["https://history.example/older"],
        )
    assert len(research_calls) == 1
    run_lines = [
        record.getMessage()
        for record in caplog.records
        if "[research] run" in record.getMessage()
    ]
    assert len(run_lines) == 1, "exactly one line per research run"
    line = run_lines[0]
    assert "ok=True" in line
    assert "sources=2" in line
    assert "history_urls=2" in line
    assert "query_chars=16" in line
    assert "duration_ms=" in line
    for record in caplog.records:
        message = record.getMessage()
        assert "retail leads" not in message
        assert "NRW" not in message
        assert "example.com" not in message
        assert "history.example" not in message


@pytest.mark.asyncio
async def test_decision_line_carries_query_length_only(tmp_path, monkeypatch, caplog):
    """The decision log gains query_chars and never the query text."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, _decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload='{"research": true, "query": "retail leads NRW"}',
    )
    prepared = PreparedExternal()
    with caplog.at_level(logging.INFO):
        await watcher._apply_research(prepared, "research retail leads please", [])
    assert len(research_calls) == 1
    decision_lines = [
        record.getMessage()
        for record in caplog.records
        if "research decision" in record.getMessage()
    ]
    assert decision_lines and "query_chars=16" in decision_lines[0]
    for record in caplog.records:
        assert "retail leads" not in record.getMessage()
        assert "NRW" not in record.getMessage()


@pytest.mark.asyncio
async def test_classifier_none_reply_becomes_honest_limitation(tmp_path, monkeypatch):
    """A None classifier reply (timeout shape) retries once, then is honest."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload=None
    )
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "find us retail leads in the US", []
    )
    assert len(classifier_calls) == 2
    assert research_calls == []
    assert researched is False
    assert sources == ()
    assert prepared.research_failure == "classifier_unavailable"
    assert decisions[0].classifier_failure is True


@pytest.mark.asyncio
async def test_classifier_retry_once_recovers_research(tmp_path, monkeypatch):
    """One strict-JSON retry recovers a usable verdict and research runs."""
    watcher = _research_watcher(tmp_path)
    calls: list[str] = []
    research_calls: list[dict] = []

    async def classify(message: str) -> str | None:
        calls.append(message)
        if len(calls) == 1:
            return _CLASSIFIER_NARRATION
        return '{"research": true, "query": "retail sector leads US"}'

    async def research(query, urls, *, max_sources, timeout=None):
        research_calls.append({"query": query})
        return ResearchResult(True, summary="fetched", sources=("https://example.com",))

    monkeypatch.setattr(watcher._runner, "classify_web_request", classify)
    monkeypatch.setattr(watcher._runner, "run_web_research", research)
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "find us retail leads", []
    )
    assert len(calls) == 2
    assert researched is True
    assert research_calls[0]["query"] == "retail sector leads US"


@pytest.mark.asyncio
async def test_valid_false_verdict_does_not_retry(tmp_path, monkeypatch):
    """A parseable research=false verdict is final: one call, no failure flag."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared, "what does invoice_service do", []
    )
    assert classifier_calls == ["what does invoice_service do"]
    assert prepared.research_failure is None
    assert decisions[0].classifier_failure is False
    assert researched is False


# --- deterministic plain-language trigger (no classifier) --------------------


_OWNER_LEAD_MESSAGE = (
    "Please search the internet and find us potential leads, "
    "target the retail sector in the US"
)


@pytest.mark.asyncio
async def test_plain_language_web_request_runs_research_with_classifier_query():
    """The exact missed owner message is decided by the classifier, not the brief."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return (
            '{"research": true, "query": "retail sector leads US", '
            '"inferred": false}'
        )

    decision = await decide_research(
        _OWNER_LEAD_MESSAGE, enabled=True, complete=complete
    )
    assert decision.research is True
    assert decision.forced is True
    assert decision.refusal is None
    assert decision.classifier_failure is False
    assert decision.query == "retail sector leads US"
    assert calls == [_OWNER_LEAD_MESSAGE]


@pytest.mark.parametrize(
    "message",
    [
        "google potential retail leads in the US",
        "look up mechanical parts suppliers online",
        "find us suppliers on the internet",
        "research the retail sector online",
        "search online for retail leads",
        "SEARCH THE WEB for retail leads",
        "look it up online",
    ],
)
@pytest.mark.asyncio
async def test_plain_language_variants_get_the_classifier_query(message):
    """Every explicit plain-language phrasing researches the model's query."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return (
            '{"research": true, "query": "retail sector leads US", '
            '"inferred": false}'
        )

    decision = await decide_research(message, enabled=True, complete=complete)
    assert decision.research is True
    assert decision.forced is True
    assert decision.query == "retail sector leads US"
    assert calls == [message]


@pytest.mark.asyncio
async def test_plain_language_internal_subject_refuses_without_classifier():
    """A repository-internal subject stays refused, exactly like !search refuses it."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": true, "query": "leaked"}'

    decision = await decide_research(
        "search the internet for this repo's tests", enabled=True, complete=complete
    )
    assert decision.research is False
    assert decision.forced is True
    assert decision.refusal == REFUSAL_AMBIGUOUS
    assert calls == []


@pytest.mark.asyncio
async def test_plain_language_private_subject_refuses_without_classifier():
    """A private-referential subject never becomes an outbound query."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": true, "query": "the token above"}'

    decision = await decide_research(
        "search the web for the token above", enabled=True, complete=complete
    )
    assert decision.research is False
    assert decision.forced is True
    assert decision.refusal == REFUSAL_PRIVATE
    assert calls == []


@pytest.mark.asyncio
async def test_plain_language_request_when_disabled_refuses():
    """Disabled mode answers an explicit web request with the honest refusal."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": true, "query": "x"}'

    decision = await decide_research(
        _OWNER_LEAD_MESSAGE, enabled=False, complete=complete
    )
    assert decision.research is False
    assert decision.forced is True
    assert decision.refusal == REFUSAL_DISABLED
    assert calls == []


@pytest.mark.asyncio
async def test_owner_lead_message_runs_research_with_classifier_query(
    tmp_path, monkeypatch, caplog
):
    """End to end: the deterministic trigger researches the classifier's query."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload=(
            '{"research": true, "query": "retail sector leads US", "inferred": false}'
        ),
    )
    prepared = PreparedExternal()
    with caplog.at_level(logging.INFO):
        sources, researched = await watcher._apply_research(
            prepared, _OWNER_LEAD_MESSAGE, []
        )
    assert classifier_calls == [_OWNER_LEAD_MESSAGE]
    assert researched is True
    assert research_calls[0]["query"] == "retail sector leads US"
    assert decisions[0].forced is True
    decision_lines = [
        record.getMessage()
        for record in caplog.records
        if "research decision" in record.getMessage()
    ]
    assert decision_lines, "the decision must be logged"
    assert "research=True" in decision_lines[0]
    assert "forced=True" in decision_lines[0]
    assert "retail" not in decision_lines[0]


@pytest.mark.asyncio
async def test_classifier_agent_gets_steps_to_answer_after_denied_tool(
    tmp_path, monkeypatch
):
    """classify_web_request gives the tool-free agent room to answer (steps=3).

    With every action denied, one denied tool attempt used to consume the only
    step, so the classifier returned step-limit narration instead of JSON.
    """
    captured: dict = {}

    def fake_build(model, prompt, *, steps=1):
        captured["steps"] = steps
        return {"agents": {}}

    monkeypatch.setenv("HOME", str(tmp_path / "operator"))
    monkeypatch.setattr("oi_agent.opencode.runner.build_tool_free_agent", fake_build)
    monkeypatch.setattr(
        "oi_agent.opencode.runner.resolve_runtime_binary",
        lambda binary, paths: "/nonexistent/oi-fake-binary",
    )
    runner = OpenCodeRunner(Config())
    result = await runner.classify_web_request("what does invoice_service do")
    assert result is None  # the fake binary never spawns; only the config is captured
    assert captured["steps"] == 3


# --- context-inferred search query (P2) ---------------------------------------


_RECENT_ICP = [
    "we're targeting D2C brands using Gorgias",
    "ICP is D2C brands on Gorgias with 50+ seats",
]
_ASK_WITHOUT_TOPIC = "can you research please, Dwight is being a bitch"


@pytest.mark.asyncio
async def test_bare_research_ask_infers_query_from_recent_messages():
    """A research ask with no topic reads the recent messages for the query."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return (
            '{"research": true, "query": "ICP leads D2C brands using Gorgias", '
            '"inferred": true}'
        )

    decision = await decide_research(
        _ASK_WITHOUT_TOPIC,
        enabled=True,
        complete=complete,
        recent_messages=[*_RECENT_ICP, _ASK_WITHOUT_TOPIC],
    )
    assert decision.research is True
    assert decision.forced is False
    assert decision.query == "ICP leads D2C brands using Gorgias"
    assert decision.inferred is True
    assert len(calls) == 1
    assert "CURRENT MESSAGE" in calls[0]
    assert "RECENT MESSAGES" in calls[0]
    for line in (*_RECENT_ICP, _ASK_WITHOUT_TOPIC):
        assert line in calls[0]


@pytest.mark.asyncio
async def test_forced_search_insult_searches_context_not_the_insult():
    """12:59 UTC shape: a forced !search without a topic searches the context."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return (
            '{"research": true, "query": "ICP leads D2C brands using Gorgias", '
            '"inferred": true}'
        )

    message = (
        "!search you both are bitches I swear. research properly, "
        "you have access to internet"
    )
    argument = message[len("!search "):]
    decision = await decide_research(
        message, enabled=True, complete=complete, recent_messages=_RECENT_ICP
    )
    assert decision.research is True
    assert decision.forced is True
    assert decision.query == "ICP leads D2C brands using Gorgias"
    assert len(calls) == 1
    assert argument in calls[0]
    for line in _RECENT_ICP:
        assert line in calls[0]


@pytest.mark.asyncio
async def test_search_ask_without_inferable_topic_refuses_ambiguous():
    """Nothing searchable in message or context asks 'search for what?'."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": true, "query": "", "inferred": true}'

    forced = await decide_research(
        "!search whatever", enabled=True, complete=complete
    )
    assert forced.research is False
    assert forced.forced is True
    assert forced.refusal == REFUSAL_AMBIGUOUS

    natural = await decide_research(
        "can you research please", enabled=True, complete=complete
    )
    assert natural.research is False
    assert natural.refusal == REFUSAL_AMBIGUOUS

    async def false_complete(text: str) -> str:
        return '{"research": false, "query": ""}'

    false_verdict = await decide_research(
        "!search nonsense", enabled=True, complete=false_complete,
    )
    assert false_verdict.research is False
    assert false_verdict.refusal == REFUSAL_AMBIGUOUS


@pytest.mark.asyncio
async def test_forced_command_classifier_failure_searches_cleaned_argument():
    """A forced command falls back to its own argument when the model fails."""
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return _CLASSIFIER_NARRATION

    decision = await decide_research(
        "!search mechanical parts suppliers from brand A and brand B",
        enabled=True,
        complete=complete,
    )
    assert decision.research is True
    assert decision.forced is True
    assert decision.refusal is None
    assert decision.classifier_failure is False
    assert decision.query == "mechanical parts suppliers from brand A and brand B"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_fetch_url_command_never_calls_the_classifier():
    calls: list[str] = []

    async def complete(text: str) -> str:
        calls.append(text)
        return '{"research": false, "query": ""}'

    decision = await decide_research(
        "!fetch https://www.python.org/downloads/", enabled=True, complete=complete
    )
    assert decision.research is True
    assert decision.forced is True
    assert decision.query == "https://www.python.org/downloads/"
    assert calls == []


@pytest.mark.asyncio
async def test_inferred_private_referential_query_refuses():
    """A context-inferred query that turns private never reaches a search."""
    async def complete(text: str) -> str:
        return (
            '{"research": true, "query": "the token from the last message", '
            '"inferred": true}'
        )

    decision = await decide_research(
        "can you research please",
        enabled=True,
        complete=complete,
        recent_messages=["the token from the last message was rotated"],
    )
    assert decision.research is False
    assert decision.refusal == REFUSAL_PRIVATE


@pytest.mark.asyncio
async def test_research_run_receives_classifier_query_not_chat_text(
    tmp_path, monkeypatch
):
    """The runner's query is the classifier's words, never message or history."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, _decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload=(
            '{"research": true, "query": "ICP leads D2C brands", "inferred": true}'
        ),
    )
    prepared = PreparedExternal()
    history = [*_RECENT_ICP]
    sources, researched = await watcher._apply_research(
        prepared, _ASK_WITHOUT_TOPIC, history
    )
    assert researched is True
    assert len(classifier_calls) == 1
    assert "CURRENT MESSAGE" in classifier_calls[0]
    for line in history:
        assert line in classifier_calls[0]
    assert research_calls[0]["query"] == "ICP leads D2C brands"
    for chat_text in (*history, _ASK_WITHOUT_TOPIC):
        assert research_calls[0]["query"] != chat_text


@pytest.mark.asyncio
async def test_context_note_records_the_search_query(tmp_path, monkeypatch):
    """A successful run records what was searched for in the evidence notes."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload=(
            '{"research": true, "query": "ICP leads D2C brands", "inferred": true}'
        ),
    )
    prepared = PreparedExternal()
    await watcher._apply_research(prepared, _ASK_WITHOUT_TOPIC, [*_RECENT_ICP])
    assert decisions[0].inferred is True
    notes = " ".join(prepared.inputs.notes)
    assert "ICP leads D2C brands" in notes
    assert "inferred" in notes
    assert _ASK_WITHOUT_TOPIC not in notes


# --- history URLs only when asked, never from bots (P4) -----------------------


@pytest.mark.asyncio
async def test_plain_topic_with_history_link_hands_no_urls_to_runner(
    tmp_path, monkeypatch
):
    """A plain topic researches the query without any history URL."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, _decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload=(
            '{"research": true, "query": "oi agent features", "inferred": false}'
        ),
    )
    prepared = PreparedExternal()
    await watcher._apply_research(
        prepared,
        "what is the oi agent about",
        ["https://github.com/umiddey/oi-agent its open source, so enjoy"],
    )
    assert research_calls[0]["urls"] == []


@pytest.mark.asyncio
async def test_current_message_url_is_a_cue_for_human_history(tmp_path, monkeypatch):
    """A URL in the current message cues history URLs too; bot-only drops."""
    watcher = _research_watcher(tmp_path)
    _classifier, research_calls, _decisions = _install_research_spies(
        monkeypatch,
        watcher,
        classifier_payload=(
            '{"research": true, "query": "example page summary", "inferred": false}'
        ),
    )
    prepared = PreparedExternal()
    await watcher._apply_research(
        prepared,
        "summarize https://example.com/page please",
        [
            "https://history.example/older-post",
            "https://bot.example/requoted",
        ],
        history_authors_bot=[False, True],
    )
    assert research_calls[0]["urls"] == [
        "https://example.com/page",
        "https://history.example/older-post",
    ]


@pytest.mark.asyncio
async def test_link_ask_passes_human_links_and_drops_bot_links(
    tmp_path, monkeypatch
):
    """A link ask fetches human-posted history links; bot-posted ones drop."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, _decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    prepared = PreparedExternal()
    await watcher._apply_research(
        prepared,
        "check these links",
        [
            "https://example.com/human",
            "https://bot.example/requoted",
        ],
        history_authors_bot=[False, True],
    )
    assert classifier_calls == []
    assert research_calls[0]["urls"] == ["https://example.com/human"]
    assert "https://bot.example/requoted" not in research_calls[0]["urls"]


@pytest.mark.asyncio
async def test_bot_only_links_do_not_force_research(tmp_path, monkeypatch):
    """Links named with only bot-posted history URLs stay a classifier ask."""
    watcher = _research_watcher(tmp_path)
    classifier_calls, research_calls, decisions = _install_research_spies(
        monkeypatch, watcher, classifier_payload='{"research": false, "query": ""}'
    )
    prepared = PreparedExternal()
    sources, researched = await watcher._apply_research(
        prepared,
        "go through these links",
        ["https://bot.example/requoted"],
        history_authors_bot=[True],
    )
    assert len(classifier_calls) == 1
    assert decisions[0].forced is False
    assert decisions[0].research is False
    assert research_calls == []
    assert researched is False
