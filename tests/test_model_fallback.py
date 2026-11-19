"""Ordered model fallback chain: which failures retry, and within what budget."""

from __future__ import annotations

import logging
import time

import pytest
from test_docs_only_write import _git_repo

from oi_agent.config import Config, WatchTarget
from oi_agent.external.research import marker_retry_needed
from oi_agent.opencode import runner as runner_module
from oi_agent.opencode.native import NativeCompletion
from oi_agent.opencode.prompt import FINAL_OUTPUT_MARKER, OPEN_CHANNEL_SILENT
from oi_agent.opencode.runner import OpenCodeRunner
from oi_agent.store import Store

PRIMARY = "p/m1"
FALLBACKS = ["p/m2", "p/m3"]


def _success_events(answer="hello"):
    """A completed native answer whose text is ``answer``."""
    return [
        {
            "type": "text",
            "sessionID": "s",
            "part": {
                "id": "p1",
                "messageID": "m1",
                "type": "text",
                "text": f"{FINAL_OUTPUT_MARKER}\n{answer}",
            },
        },
        {
            "type": "step_finish",
            "sessionID": "s",
            "part": {
                "id": "p2",
                "messageID": "m1",
                "type": "step-finish",
                "reason": "stop",
            },
        },
    ]


def _success(answer="hello"):
    return (_success_events(answer), 0, "", 0, None, 5, NativeCompletion(True, None, "m1"))


def _provider_error(status=429):
    event = {
        "type": "error",
        "sessionID": "s",
        "error": {
            "type": "provider.rate-limit" if status == 429 else "provider.internal",
            "message": "rate limited",
            "status": status,
        },
    }
    return ([event], 0, "", 1, "nonzero_exit", 5, NativeCompletion(False, "nonzero_exit"))


def _timeout():
    return ([], 0, "", None, "timeout", 5, NativeCompletion(False, "timeout"))


def _non_provider(error_class):
    return ([], 0, "", 1, error_class, 5, NativeCompletion(False, error_class))


class _Clock:
    """Stands in for the ``time`` module inside the runner; only the clock is faked."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 0.0

    def __getattr__(self, name):
        return getattr(time, name)


def _config(**overrides):
    cfg = Config(
        opencode_binary="/bin/true",
        opencode_model=PRIMARY,
        opencode_fallback_models=list(FALLBACKS),
        opencode_timeout_seconds=5,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


async def _run(tmp_path, monkeypatch, cfg, outcomes):
    """Run the chain with ``_execute_agent`` replaced by ``outcomes`` in call order."""
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(runner_module, "resolve_runtime_binary", lambda *args, **kwargs: "/bin/true")
    store = Store(tmp_path / "state.db")
    runner = OpenCodeRunner(cfg, store)
    calls = []
    queue = list(outcomes)

    async def fake_execute(**kwargs):
        calls.append(kwargs)
        outcome = queue.pop(0)
        if callable(outcome):
            return outcome()
        return outcome

    monkeypatch.setattr(runner, "_execute_agent", fake_execute)
    target = WatchTarget(111, str(repo), 99, 3)
    try:
        reply = await runner._run_locked(repo, [], target, "alice", "what changed?", "")
    finally:
        store.close()
    return reply, calls


@pytest.mark.asyncio
async def test_primary_answer_skips_fallbacks(tmp_path, monkeypatch):
    """A successful primary answer never calls a fallback."""
    reply, calls = await _run(tmp_path, monkeypatch, _config(), [_success()])
    assert reply.ok
    assert reply.text == "hello"
    assert reply.model == PRIMARY
    assert [c["model"] for c in calls] == [PRIMARY]


@pytest.mark.asyncio
async def test_provider_error_moves_to_next_model(tmp_path, monkeypatch):
    """A 429 on the primary retries the next configured model, which answers."""
    reply, calls = await _run(tmp_path, monkeypatch, _config(), [_provider_error(429), _success()])
    assert reply.ok
    assert reply.text == "hello"
    assert reply.model == FALLBACKS[0]
    assert [c["model"] for c in calls] == [PRIMARY, FALLBACKS[0]]


@pytest.mark.asyncio
async def test_all_models_fail_gives_one_failure_and_logs_each_attempt(tmp_path, monkeypatch, caplog):
    """Every model failing yields one failure reply; each attempt is logged with model and class."""
    caplog.set_level(logging.INFO)
    reply, calls = await _run(
        tmp_path,
        monkeypatch,
        _config(),
        [_provider_error(429), _provider_error(503), _provider_error(500)],
    )
    assert not reply.ok
    assert [c["model"] for c in calls] == [PRIMARY, *FALLBACKS]
    for model in (PRIMARY, *FALLBACKS):
        assert any(f"model={model} " in rec.getMessage() for rec in caplog.records), model


@pytest.mark.asyncio
@pytest.mark.parametrize("error_class", ["nonzero_exit", "empty_response"])
async def test_non_provider_failure_does_not_retry(tmp_path, monkeypatch, error_class):
    """A failure that is not a provider error keeps the single primary attempt."""
    reply, calls = await _run(tmp_path, monkeypatch, _config(), [_non_provider(error_class)])
    assert not reply.ok
    assert [c["model"] for c in calls] == [PRIMARY]


@pytest.mark.asyncio
async def test_default_config_runs_primary_with_configured_timeout(tmp_path, monkeypatch):
    """With no fallback list the single attempt is the old call: primary model, configured timeout."""
    reply, calls = await _run(tmp_path, monkeypatch, _config(opencode_fallback_models=[]), [_success()])
    assert reply.ok
    assert [(c["model"], c["timeout_seconds"]) for c in calls] == [(PRIMARY, 5)]


@pytest.mark.asyncio
async def test_timeout_class_is_retryable(tmp_path, monkeypatch):
    """A timeout retries the next model while budget remains, with the remaining budget as its timeout."""
    clock = _Clock()
    monkeypatch.setattr(runner_module, "time", clock)

    def slow_timeout():
        clock.now += 6
        return _timeout()

    cfg = _config(opencode_fallback_total_timeout_seconds=10)
    reply, calls = await _run(tmp_path, monkeypatch, cfg, [slow_timeout, _success()])
    assert reply.ok
    assert [c["timeout_seconds"] for c in calls] == [5, 4.0]


@pytest.mark.asyncio
async def test_timeout_retries_only_while_budget_remains(tmp_path, monkeypatch):
    """No fallback starts once less than one second of the total budget is left."""
    clock = _Clock()
    monkeypatch.setattr(runner_module, "time", clock)

    def slow_timeout():
        clock.now += 9.5
        return _timeout()

    cfg = _config(opencode_fallback_total_timeout_seconds=10)
    reply, calls = await _run(tmp_path, monkeypatch, cfg, [slow_timeout])
    assert not reply.ok
    assert [c["model"] for c in calls] == [PRIMARY]


@pytest.mark.asyncio
async def test_provider_exhaustion_is_not_a_marker_retry(tmp_path, monkeypatch):
    """After every model fails on provider errors, the marker format retry must not run."""
    reply, _ = await _run(
        tmp_path,
        monkeypatch,
        _config(),
        [_provider_error(429), _provider_error(429), _provider_error(429)],
    )
    assert not marker_retry_needed(error_class=reply.error_class, had_content=reply.had_content)


@pytest.mark.asyncio
async def test_fallback_answer_carries_no_model_footer(tmp_path, monkeypatch):
    """A fallback answer is posted as-is: no model name is appended, and the cap holds."""
    cfg = _config(max_reply_chars=200)
    reply, _ = await _run(tmp_path, monkeypatch, cfg, [_provider_error(429), _success("x" * 500)])
    assert len(reply.text) <= 200
    assert "answered by" not in reply.text
    assert reply.model == FALLBACKS[0]


@pytest.mark.asyncio
async def test_silent_fallback_answer_has_no_footer(tmp_path, monkeypatch):
    """A silent answer from a fallback stays silent: no footer is added."""
    reply, _ = await _run(tmp_path, monkeypatch, _config(), [_provider_error(429), _success(OPEN_CHANNEL_SILENT)])
    assert reply.ok
    assert reply.text == OPEN_CHANNEL_SILENT
    assert reply.model == FALLBACKS[0]


@pytest.mark.asyncio
async def test_total_failure_has_no_footer_or_model(tmp_path, monkeypatch):
    """A chain that never answers carries no answering model and no footer."""
    reply, _ = await _run(tmp_path, monkeypatch, _config(), [_provider_error(429)] * 3)
    assert not reply.ok
    assert reply.model is None
    assert "answered by" not in reply.text


@pytest.mark.asyncio
async def test_empty_fallback_list_is_the_unchanged_single_model_path(tmp_path, monkeypatch):
    """Golden: with no fallback list, one primary call, the primary timeout, no footer."""
    cfg = _config(opencode_fallback_models=[])
    reply, calls = await _run(tmp_path, monkeypatch, cfg, [_success()])
    assert reply.ok
    assert reply.text == "hello"
    assert reply.model == PRIMARY
    assert reply.attachments == ()
    assert len(calls) == 1
    assert calls[0]["model"] == PRIMARY
    assert calls[0]["timeout_seconds"] == cfg.opencode_timeout_seconds
