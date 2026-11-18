"""Open-channel prompt tests, plus a golden guard that non-open prompts stay byte-identical."""

from __future__ import annotations

import json
from pathlib import Path

from oi_agent.dynamics import MemorySnapshot, ProfileView
from oi_agent.opencode.prompt import build_prompt

GOLDEN = Path(__file__).parent / "fixtures" / "build_prompt_golden.json"
_GENERATED_AT = "2026-01-01T00:00:00+00:00"


def golden_cases() -> dict[str, dict]:
    """Non-open ``build_prompt`` inputs whose full output is pinned in GOLDEN."""
    view = ProfileView(
        member_id="m1",
        handle="member",
        directness_band="high",
        detail_preference="brief",
        challenge_preference="direct",
        humor_preference="medium",
        decision_style="options",
        recurring_topics=("runner",),
        confidence=0.9,
    )
    empty = MemorySnapshot(
        profiles=(), transient=(), team_pulse=None, calibration=None, generated_at=_GENERATED_AT
    )
    full = MemorySnapshot(
        profiles=(view,), transient=(), team_pulse=None, calibration=None, generated_at=_GENERATED_AT
    )
    return {
        "default": {},
        "personality": {"personality": "Speak like a terse reviewer"},
        "server_env": {"environment_mode": "server", "primary_repo": "/repo"},
        "docs_only": {
            "primary_repo": "/repo",
            "write_mode": "docs-only",
            "write_dirs": ["docs"],
        },
        "multi_repo": {"primary_repo": "/repo", "additional_repos": ["/aux1", "/aux2"]},
        "identity_name_and_id": {"bot_name": "Oi Bot", "bot_id": 1234567890},
        "identity_name_only": {"bot_name": "Oi Bot"},
        "identity_id_only": {"bot_id": 1234567890},
        "memory_full": {"primary_repo": "/repo", "memory": full},
        "memory_empty": {"primary_repo": "/repo", "memory": empty},
        "external_inputs": {"primary_repo": "/repo", "external_inputs": True},
    }


def test_non_open_prompts_byte_identical():
    """Non-open prompts match the pinned output exactly."""
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = golden_cases()
    assert expected.keys() == cases.keys()
    for name, kwargs in cases.items():
        assert build_prompt(**kwargs) == expected[name], name
