"""Provider error classification from the recorded OpenCode event shapes.

The captured events come from tests/fixtures/provider_errors/README.md: one
error event per run, with ``error.type`` and an integer ``error.status``.
Only the integer status decides retryability; ``error.message`` is never read.
"""

from __future__ import annotations

import pytest

from oi_agent.opencode.runner import (
    RETRYABLE_CLASSES,
    _provider_class,
    _provider_status,
    _retry_class,
)


def _captured(error_type: str, status: object) -> dict[str, object]:
    return {
        "type": "error",
        "timestamp": 1,
        "sessionID": "ses_fixture",
        "error": {"type": error_type, "message": "fixture", "status": status},
    }


CAPTURED = [
    (_captured("provider.rate-limit", 429), 429, "provider_rate_limited"),
    (_captured("provider.internal", 500), 500, "provider_server_error"),
    (_captured("provider.internal", 502), 502, "provider_unavailable"),
    (_captured("provider.internal", 503), 503, "provider_unavailable"),
    (_captured("provider.internal", 504), 504, "provider_unavailable"),
    (_captured("provider.internal", 599), 599, "provider_server_error"),
]


@pytest.mark.parametrize(("event", "status", "klass"), CAPTURED)
def test_captured_shapes_map_to_their_class(event: dict[str, object], status: int, klass: str) -> None:
    assert _provider_status(event) == status
    assert _provider_class(status) == klass
    assert _retry_class("provider.rate-limit", [event]) == klass


def test_bad_request_is_not_retryable() -> None:
    # The unknown-model stand-in arrives as a bare 400 (README).
    event = _captured("provider.invalid-request", 400)
    assert _provider_status(event) == 400
    assert _provider_class(400) is None
    assert _retry_class("provider.invalid-request", [event]) is None


@pytest.mark.parametrize(
    "event",
    [
        _captured("provider.internal", 0),
        _captured("provider.internal", 99),
        _captured("provider.internal", 600),
        _captured("provider.internal", True),
        _captured("provider.internal", False),
        _captured("provider.internal", "429"),
        _captured("provider.internal", 429.0),
        _captured("provider.internal", None),
        {"type": "error", "error": {"type": "provider.rate-limit"}},
        {"type": "error", "error": "429 rate limit"},
        {"type": "error"},
    ],
)
def test_status_helper_rejects_hostile_or_out_of_range(event: dict[str, object]) -> None:
    assert _provider_status(event) is None
    assert _retry_class("opencode_error", [event]) is None


def test_message_text_never_counts_as_status() -> None:
    event = {
        "type": "error",
        "error": {"type": "provider.internal", "message": "HTTP 429 rate limit"},
    }
    assert _provider_status(event) is None
    assert _retry_class("provider.internal", [event]) is None


def test_timeout_class_is_retryable() -> None:
    assert "timeout" in RETRYABLE_CLASSES
    assert _retry_class("timeout", []) in RETRYABLE_CLASSES


def test_retryable_set_is_the_allowlist() -> None:
    assert set(RETRYABLE_CLASSES) == {
        "provider_rate_limited",
        "provider_unavailable",
        "provider_server_error",
        "timeout",
    }


@pytest.mark.parametrize("error_class", ["nonzero_exit", "prompt_too_large", "empty_response"])
def test_non_provider_failures_are_not_retryable(error_class: str) -> None:
    assert _retry_class(error_class, []) not in RETRYABLE_CLASSES
