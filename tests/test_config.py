"""Tests for the OpenCode-only configuration contract."""

import shutil

import pytest
from typer.testing import CliRunner

from oi_agent.config import (
    Config,
    WatchTarget,
    load_config,
    save_config,
    validate_config,
)


def _binary() -> str:
    """Return a portable executable accepted by config validation."""
    return shutil.which("true") or "/bin/true"


def _valid_config() -> Config:
    """Build a valid isolated test configuration."""
    cfg = Config(opencode_binary=_binary(), opencode_model="provider/model")
    cfg.watches = [WatchTarget(channel_id=111, repo_path="/srv/repo")]
    return cfg


def test_roundtrip_is_opencode_only(tmp_path):
    """New settings round-trip and contain no provider credentials."""
    cfg = _valid_config()
    cfg.personality = 'Warm "expert"\nUse bullets.'
    cfg.opencode_steps = 50
    cfg.opencode_timeout_seconds = 1200
    path = tmp_path / "config.toml"

    save_config(cfg, path)
    back = load_config(path)

    assert back.opencode_model == "provider/model"
    assert back.opencode_steps == 50
    assert back.opencode_timeout_seconds == 1200
    assert back.personality == cfg.personality
    assert back.watches[0].channel_id == 111
    assert "api_key" not in path.read_text()
    assert "base_url" not in path.read_text()


def test_legacy_harness_keys_fail_with_migration_message(tmp_path):
    """Old provider/tool-loop settings fail instead of being ignored."""
    path = tmp_path / "legacy.toml"
    path.write_text(
        '[agent]\nbase_url = "https://example.invalid"\n'
        '[[watch]]\nchannel_id = 1\nrepo_path = "/repo"\n'
        'search_aliases = { bug = ["defect"] }\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="rerun `oi init`"):
        load_config(path)
def test_missing_watch_and_missing_required_field_rejected(tmp_path):
    """A daemon config must contain at least one complete watch target."""
    missing = tmp_path / "missing.toml"
    missing.write_text(
        "[[watch]]\nchannel_id = 1\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="repo_path"):
        load_config(missing)

    empty = tmp_path / "empty.toml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="watch"):
        load_config(empty)


def test_invalid_model_binary_and_limits_rejected(tmp_path):
    """Provider/model shape, executable, steps, and timeout are bounded."""
    cfg = _valid_config()
    cfg.opencode_model = "bare-model"
    with pytest.raises(ValueError, match="provider/model"):
        save_config(cfg, tmp_path / "bad-model.toml")

    cfg = _valid_config()
    cfg.opencode_binary = str(tmp_path / "missing-opencode")
    with pytest.raises(ValueError, match="not executable"):
        save_config(cfg, tmp_path / "bad-binary.toml")

    for field, value in (("opencode_steps", 51),
                         ("opencode_timeout_seconds", 86_401)):
        cfg = _valid_config()
        setattr(cfg, field, value)
        with pytest.raises(ValueError, match=field):
            save_config(cfg, tmp_path / f"bad-{field}.toml")


def test_fallback_chain_roundtrips_and_is_bounded(tmp_path):
    """Fallback ids round-trip in order; repeats, the primary, bad shape, and over five are rejected."""
    assert _valid_config().opencode_fallback_models == []
    assert _valid_config().opencode_fallback_total_timeout_seconds == 0
    cfg = _valid_config()
    cfg.opencode_fallback_models = ["other/one", "other/two"]
    cfg.opencode_fallback_total_timeout_seconds = 600
    path = tmp_path / "fallback.toml"

    save_config(cfg, path)
    back = load_config(path)

    assert back.opencode_fallback_models == ["other/one", "other/two"]
    assert back.opencode_fallback_total_timeout_seconds == 600

    for index, (models, match) in enumerate((
        (["other/one", "other/one"], "must not repeat entries"),
        (["provider/model"], "must not repeat opencode_model"),
        (["bare"], "exact provider/model format"),
        ([f"p/m{n}" for n in range(6)], "allows at most 5 entries"),
        (["other/bad id"], "may use only letters"),
        (["-leading/one"], "may use only letters"),
        ([".dot/one"], "may use only letters"),
        (["p/" + "a" * 99], "capped at 100 characters"),
    )):
        cfg = _valid_config()
        cfg.opencode_fallback_models = models
        with pytest.raises(ValueError, match=match):
            save_config(cfg, tmp_path / f"bad-{index}.toml")


def test_omp_model_roundtrips_and_is_validated(tmp_path):
    """omp_model round-trips, and a selector without a provider is rejected."""
    cfg = _valid_config()
    cfg.harness = "omp"
    cfg.omp_model = "anthropic/claude-haiku-5-5"
    cfg.opencode_fallback_models = ["other/one"]
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.omp_model == "anthropic/claude-haiku-5-5"
    assert loaded.opencode_fallback_models == ["other/one"]
    cfg.omp_model = "no-provider"
    with pytest.raises(ValueError, match="omp_model"):
        save_config(cfg, tmp_path / "bad.toml")


def test_atomic_save_preserves_previous_bytes_on_validation_failure(tmp_path):
    """Validation happens before the atomic replacement and never clobbers."""
    path = tmp_path / "config.toml"
    save_config(_valid_config(), path)
    before = path.read_bytes()

    invalid = _valid_config()
    invalid.max_reply_chars = 1
    with pytest.raises(ValueError):
        save_config(invalid, path)

    assert path.read_bytes() == before
    assert not (tmp_path / "config.toml.tmp").exists()


def test_single_message_limit_and_target_resolution(tmp_path):
    """Retained Discord delivery constraints still validate in the new schema."""
    cfg = _valid_config()
    cfg.reply_delivery = "single_message"
    cfg.max_reply_chars = 2000
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert load_config(path).target_for_channel(111).repo_path == "/srv/repo"

    cfg.max_reply_chars = 2001
    with pytest.raises(ValueError, match="single_message"):
        save_config(cfg, tmp_path / "too-large.toml")


def test_environment_mode_roundtrip_for_both_values(tmp_path):
    """workstation and server both survive a validated TOML round-trip."""
    for mode in ("workstation", "server"):
        cfg = _valid_config()
        cfg.environment_mode = mode
        path = tmp_path / f"config-{mode}.toml"
        save_config(cfg, path)
        assert load_config(path).environment_mode == mode


def test_environment_mode_invalid_value_rejected(tmp_path):
    """Only workstation | server are valid; anything else fails validation."""
    cfg = _valid_config()
    cfg.environment_mode = "hybrid"
    with pytest.raises(ValueError, match="environment_mode"):
        save_config(cfg, tmp_path / "bad-mode.toml")


def test_old_config_without_harness_keys_loads_opencode(tmp_path):
    """Configs written before the harness keys load with the safe defaults."""
    path = tmp_path / "old.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert cfg.harness == "opencode"
    assert cfg.omp_binary == "omp"


def test_harness_omp_roundtrip_and_binary_required(tmp_path):
    """harness=omp survives save/load; omp_binary must resolve in that mode."""
    fake_omp = tmp_path / "fake-omp"
    fake_omp.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_omp.chmod(fake_omp.stat().st_mode | 0o111)

    cfg = _valid_config()
    cfg.harness = "omp"
    cfg.omp_binary = str(fake_omp)
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.harness == "omp"
    assert back.omp_binary == str(fake_omp)

    missing = _valid_config()
    missing.harness = "omp"
    missing.omp_binary = str(tmp_path / "missing-omp")
    with pytest.raises(ValueError, match="omp_binary"):
        save_config(missing, tmp_path / "bad-omp.toml")


def test_harness_invalid_value_rejected(tmp_path):
    """Only opencode | omp are valid harnesses."""
    cfg = _valid_config()
    cfg.harness = "claude"
    with pytest.raises(ValueError, match="harness"):
        save_config(cfg, tmp_path / "bad-harness.toml")


def test_environment_mode_legacy_auto_variants_get_migration_hint(tmp_path):
    """'auto' and case/whitespace variants fail with the exact repair command."""
    for raw_value in ("auto", "AUTO", " auto ", "Auto"):
        path = tmp_path / "legacy.toml"
        path.write_text(
            f'environment_mode = "{raw_value}"\n'
            '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="oi config set environment_mode"):
            load_config(path)


def test_config_set_environment_mode_cli_roundtrip(tmp_path):
    """oi config set accepts environment_mode; show displays it; invalid rejected."""
    from oi_agent.cli import app

    runner = CliRunner()
    path = tmp_path / "config.toml"
    save_config(_valid_config(), path)

    result = runner.invoke(
        app, ["config", "set", "environment_mode", "server", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(path).environment_mode == "server"

    shown = runner.invoke(app, ["config", "show", "--config", str(path)])
    assert shown.exit_code == 0, shown.output
    assert "environment_mode = server" in shown.output

    bad = runner.invoke(
        app, ["config", "set", "environment_mode", "hybrid", "--config", str(path)]
    )
    assert bad.exit_code == 1
    assert load_config(path).environment_mode == "server"


def test_config_set_environment_mode_repairs_legacy_auto(tmp_path):
    """The documented repair command works on a legacy 'auto' config."""
    from oi_agent.cli import app

    runner = CliRunner()
    path = tmp_path / "config.toml"
    save_config(_valid_config(), path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'environment_mode = "workstation"', 'environment_mode = "auto"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="oi config set environment_mode"):
        load_config(path)

    result = runner.invoke(
        app, ["config", "set", "environment_mode", "server", "--config", str(path)]
    )
    assert result.exit_code == 0, result.output
    assert load_config(path).environment_mode == "server"


def test_session_keys_accepted_dropped_and_never_written(tmp_path):
    """Stale session-rotation keys load, drop, and are never written back."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'max_session_turns = 5\n'
        'session_ttl_days = 2\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert not hasattr(cfg, "max_session_turns")
    assert not hasattr(cfg, "session_ttl_days")
    save_config(cfg, path)
    text = path.read_text(encoding="utf-8")
    assert "max_session_turns" not in text
    assert "session_ttl_days" not in text
    # Even a stale, wrong-typed value loads: the keys are dropped, never parsed.
    path.write_text(
        'max_session_turns = "junk"\n'
        f'opencode_binary = "{_binary()}"\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )
    assert not hasattr(load_config(path), "max_session_turns")


# --- controller-owned external inputs (plan corrections 8 + 12) -----------------

def test_external_inputs_defaults_are_conservative():
    """Disabled retrieval and image inputs are the honest shipped defaults."""
    cfg = Config()
    assert cfg.external_inputs == "disabled"
    assert cfg.image_inputs == "off"
    assert cfg.max_external_evidence_items == 8
    assert cfg.max_attachments == 4
    assert cfg.max_attachment_bytes == 8 * 1024 * 1024


def test_external_inputs_roundtrip_and_validate(tmp_path):
    """Operator-enabled external inputs survive a validated round-trip."""
    cfg = _valid_config()
    cfg.external_inputs = "read-only"
    cfg.image_inputs = "on"
    cfg.max_external_evidence_items = 12
    cfg.max_attachments = 6
    cfg.max_attachment_bytes = 4 * 1024 * 1024
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.external_inputs == "read-only"
    assert back.image_inputs == "on"
    assert back.max_external_evidence_items == 12
    assert back.max_attachments == 6
    assert back.max_attachment_bytes == 4 * 1024 * 1024
    # Validation is silent on the valid combination.
    validate_config(back)


@pytest.mark.parametrize("field, value", [
    ("external_inputs", "full"),
    ("external_inputs", "write"),
    ("image_inputs", "auto"),
    ("image_inputs", "true"),
    ("max_external_evidence_items", 0),
    ("max_external_evidence_items", 51),
    ("max_attachments", -1),
    ("max_attachments", 17),
    ("max_attachment_bytes", 1023),
    ("max_attachment_bytes", 33 * 1024 * 1024),
])
def test_external_inputs_invalid_values_fail_closed(tmp_path, field, value):
    """Unknown modes and out-of-bounds caps are rejected with the repair path."""
    import json as _json

    path = tmp_path / "config.toml"
    path.write_text(
        f'{field} = {_json.dumps(value)}\n'
        f'opencode_binary = "{_binary()}"\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=field):
        load_config(path)
    # The same rejection protects an in-memory save (atomic write stays put).
    cfg = _valid_config()
    save_config(cfg, path)
    before = path.read_bytes()
    setattr(cfg, field, value)
    with pytest.raises(ValueError, match=field):
        save_config(cfg, path)
    assert path.read_bytes() == before


def test_external_inputs_cli_set_show_roundtrip(tmp_path):
    """oi config set edits the external knobs; show displays them; junk fails."""
    from oi_agent.cli import app

    runner = CliRunner()
    path = tmp_path / "config.toml"
    save_config(_valid_config(), path)

    for key, value in (
        ("external_inputs", "read-only"),
        ("image_inputs", "on"),
        ("max_attachments", "6"),
        ("max_attachment_bytes", "1048576"),
        ("max_external_evidence_items", "12"),
    ):
        result = runner.invoke(
            app, ["config", "set", key, value, "--config", str(path)]
        )
        assert result.exit_code == 0, result.output

    shown = runner.invoke(app, ["config", "show", "--config", str(path)])
    assert shown.exit_code == 0, shown.output
    assert "external_inputs = read-only" in shown.output
    assert "image_inputs = on" in shown.output
    assert "max_attachments = 6" in shown.output
    assert "max_attachment_bytes = 1048576" in shown.output
    assert "max_external_evidence_items = 12" in shown.output

    bad = runner.invoke(
        app, ["config", "set", "image_inputs", "auto", "--config", str(path)]
    )
    assert bad.exit_code == 1
    assert load_config(path).image_inputs == "on"


# --- P1: reminder_channel_id moves from Config to per-watch WatchTarget ----


def _reminder_toml(reminder_channel_id: int | None = None) -> str:
    """Build a minimal valid TOML config with one watch target.

    When ``reminder_channel_id`` is given it is placed inside the ``[[watch]]``
    table; otherwise it is omitted entirely.
    """
    lines = [
        f'opencode_binary = "{_binary()}"',
        'opencode_model = "provider/model"',
        '[[watch]]',
        'channel_id = 1',
        'repo_path = "/repo"',
    ]
    if reminder_channel_id is not None:
        lines.append(f"reminder_channel_id = {reminder_channel_id}")
    return "\n".join(lines) + "\n"


def test_reminder_channel_id_parses_per_watch(tmp_path):
    """A per-watch reminder_channel_id is loaded onto its WatchTarget."""
    path = tmp_path / "config.toml"
    path.write_text(_reminder_toml(200), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.watches[0].reminder_channel_id == 200


def test_reminder_channel_id_roundtrips(tmp_path):
    """A non-zero per-watch reminder_channel_id survives save -> load."""
    cfg = _valid_config()
    cfg.watches[0].reminder_channel_id = 200
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.watches[0].reminder_channel_id == 200
    assert "reminder_channel_id = 200" in path.read_text()


def test_reminder_channel_id_zero_not_written(tmp_path):
    """A zero per-watch reminder_channel_id is omitted from saved TOML."""
    cfg = _valid_config()
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert "reminder_channel_id" not in path.read_text()


def test_duplicate_reminder_channel_id_rejected(tmp_path):
    """Two watches may not claim the same non-zero reminder channel."""
    path = tmp_path / "dup.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nchannel_id = 1\nrepo_path = "/repo"\n'
        'reminder_channel_id = 200\n'
        '[[watch]]\nchannel_id = 2\nrepo_path = "/repo"\n'
        'reminder_channel_id = 200\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate reminder_channel_id 200"):
        load_config(path)


def test_negative_reminder_channel_id_rejected(tmp_path):
    """A negative per-watch reminder_channel_id is refused."""
    cfg = _valid_config()
    cfg.watches[0].reminder_channel_id = -1
    with pytest.raises(ValueError, match="reminder_channel_id must be a non-negative integer"):
        save_config(cfg, tmp_path / "neg.toml")


def test_nonzero_top_level_reminder_channel_refused(tmp_path):
    """A non-zero legacy top-level reminder_channel_id is refused with migration message."""
    path = tmp_path / "legacy.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        'reminder_channel_id = 200\n'
        '[[watch]]\nchannel_id = 1\nrepo_path = "/repo"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reminder_channel_id now lives in its"):
        load_config(path)


def test_top_level_reminder_channel_zero_accepted_and_dropped(tmp_path):
    """A top-level reminder_channel_id = 0 is accepted and not written back."""
    path = tmp_path / "zero.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        'reminder_channel_id = 0\n'
        '[[watch]]\nchannel_id = 1\nrepo_path = "/repo"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    save_config(cfg, path)
    assert "reminder_channel_id" not in path.read_text()


def test_is_reminder_channel_returns_owning_watch():
    """is_reminder_channel maps a reminder channel to its owning WatchTarget."""
    cfg = Config(opencode_binary=_binary(), opencode_model="provider/model")
    cfg.watches = [
        WatchTarget(channel_id=1, repo_path="/repo", reminder_channel_id=200),
        WatchTarget(channel_id=2, repo_path="/repo", reminder_channel_id=300),
    ]
    assert cfg.is_reminder_channel(200) is cfg.watches[0]
    assert cfg.is_reminder_channel(300) is cfg.watches[1]
    assert cfg.is_reminder_channel(999) is None
    assert cfg.is_reminder_channel(0) is None


# --- P1: open-channel globals ---------------------------------------------------


def test_open_channel_globals_defaults():
    """The shipped open-channel defaults: 40 messages, 6 runs/hour, opencode model."""
    cfg = Config()
    assert cfg.context_messages == 40
    assert cfg.open_channel_runs_per_hour == 6
    assert cfg.summary_model == ""
    assert cfg.summary_max_chars == 1_500
    assert cfg.summary_decay_days == 60
    assert cfg.summary_bootstrap_days == 30
    assert cfg.summary_bootstrap_messages == 300


def test_open_channel_globals_roundtrip(tmp_path):
    """Every new global survives a validated TOML round-trip."""
    cfg = _valid_config()
    cfg.context_messages = 25
    cfg.open_channel_runs_per_hour = 12
    cfg.summary_model = "provider/summarizer"
    cfg.summary_max_chars = 2_000
    cfg.summary_decay_days = 90
    cfg.summary_bootstrap_days = 14
    cfg.summary_bootstrap_messages = 150
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.context_messages == 25
    assert back.open_channel_runs_per_hour == 12
    assert back.summary_model == "provider/summarizer"
    assert back.summary_max_chars == 2_000
    assert back.summary_decay_days == 90
    assert back.summary_bootstrap_days == 14
    assert back.summary_bootstrap_messages == 150
    assert "context_messages = 25" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("field, value", [
    ("context_messages", 9),
    ("context_messages", 201),
    ("open_channel_runs_per_hour", 0),
    ("open_channel_runs_per_hour", 61),
    ("summary_max_chars", 599),
    ("summary_max_chars", 4_001),
    ("summary_decay_days", 6),
    ("summary_decay_days", 731),
    ("summary_bootstrap_days", 0),
    ("summary_bootstrap_days", 366),
    ("summary_bootstrap_messages", -1),
    ("summary_bootstrap_messages", 2_001),
    ("summary_model", "bare-model"),
    ("open_channel_prompt", "chatty"),
])
def test_open_channel_globals_out_of_range_rejected(tmp_path, field, value):
    """Out-of-range globals fail both TOML load and in-memory save."""
    import json as _json

    path = tmp_path / "bad.toml"
    path.write_text(
        f'{field} = {_json.dumps(value)}\n'
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=field):
        load_config(path)
    # The same rejection protects an in-memory save (atomic write stays put).
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    before = path.read_bytes()
    setattr(cfg, field, value)
    with pytest.raises(ValueError, match=field):
        save_config(cfg, path)
    assert path.read_bytes() == before


def test_summary_model_empty_uses_opencode_model():
    """An empty summary_model is the supported 'same model' default."""
    cfg = _valid_config()
    cfg.summary_model = ""
    validate_config(cfg)


# --- P1: per-watch open_channels ------------------------------------------------


def test_open_channels_parse_and_roundtrip(tmp_path):
    """open_channels load onto their watch and survive save -> load."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nguild_id = 222\nrepo_path = "/srv/repo"\n'
        'allowed_channels = [333, 444]\n'
        'open_channels = [333, 444]\n'
        '[[watch]]\nchannel_id = 111\nrepo_path = "/srv/repo"\n'
        'open_channels = [111]\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.watches[0].open_channels == [333, 444]
    assert cfg.watches[1].open_channels == [111]
    save_config(cfg, path)
    back = load_config(path)
    assert back.watches[0].open_channels == [333, 444]
    assert back.watches[1].open_channels == [111]
    assert "open_channels = [333, 444]" in path.read_text(encoding="utf-8")


def test_open_channels_empty_not_written(tmp_path):
    """A watch without open channels omits the key from saved TOML."""
    cfg = _valid_config()
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    assert "open_channels" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("watch_toml, message", [
    # a reminder channel intercepts first: never an open channel
    (
        '[[watch]]\nchannel_id = 1\nrepo_path = "/repo"\nreminder_channel_id = 200\n'
        '[[watch]]\nguild_id = 222\nrepo_path = "/repo"\nopen_channels = [200]\n',
        "reminder_channel_id",
    ),
    # the same channel open on two watches
    (
        '[[watch]]\nguild_id = 222\nrepo_path = "/repo"\nopen_channels = [333]\n'
        '[[watch]]\nguild_id = 223\nrepo_path = "/repo"\nopen_channels = [333]\n',
        "duplicate open_channels",
    ),
    # an ignored channel can never be open
    (
        '[[watch]]\nguild_id = 222\nrepo_path = "/repo"\n'
        'ignored_channels = [555]\nopen_channels = [555]\n',
        "ignored_channels",
    ),
    # outside the allowlist
    (
        '[[watch]]\nguild_id = 222\nrepo_path = "/repo"\n'
        'allowed_channels = [333]\nopen_channels = [444]\n',
        "allowed_channels",
    ),
    # a channel watch may only open its own channel
    (
        '[[watch]]\nchannel_id = 111\nrepo_path = "/repo"\nopen_channels = [999]\n',
        "own channel_id",
    ),
])
def test_open_channels_refusals(tmp_path, watch_toml, message):
    """Invalid open_channels configurations are refused at load."""
    path = tmp_path / "bad.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n' + watch_toml,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=message):
        load_config(path)


def test_open_channels_non_positive_item_rejected(tmp_path):
    """Non-positive open_channels entries fail at parse and at in-memory save."""
    path = tmp_path / "bad.toml"
    path.write_text(
        f'opencode_binary = "{_binary()}"\n'
        'opencode_model = "provider/model"\n'
        '[[watch]]\nguild_id = 222\nrepo_path = "/repo"\nopen_channels = [0]\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="positive"):
        load_config(path)
    cfg = _valid_config()
    cfg.watches[0].open_channels = [-1]
    with pytest.raises(ValueError, match="open_channels item must be a positive integer"):
        save_config(cfg, tmp_path / "neg.toml")


def test_open_channel_prompt_defaults_to_none():
    """Open-channel prompt mode defaults to none with no per-channel overrides."""
    assert Config().open_channel_prompt == "none"
    assert WatchTarget(channel_id=111, repo_path="/srv/repo").open_channel_prompts == {}


def test_open_channel_prompt_roundtrip_and_resolution(tmp_path):
    """Global mode and per-channel overrides survive a round-trip and resolve per channel."""
    cfg = _valid_config()
    cfg.open_channel_prompt = "personality"
    cfg.watches[0].open_channels = [111]
    cfg.watches[0].open_channel_prompts = {111: "personality"}
    path = tmp_path / "config.toml"
    save_config(cfg, path)
    back = load_config(path)
    assert back.open_channel_prompt == "personality"
    target = back.watches[0]
    assert target.open_channel_prompts == {111: "personality"}
    assert target.open_channel_prompt_for(111, "none") == "personality"
    assert target.open_channel_prompt_for(222, "none") == "none"


@pytest.mark.parametrize("prompts", [{111: "chatty"}, {222: "personality"}])
def test_open_channel_prompt_overrides_rejected(prompts):
    """An override needs a known mode and must name one of the watch's open channels."""
    cfg = _valid_config()
    cfg.watches[0].open_channels = [111]
    cfg.watches[0].open_channel_prompts = prompts
    with pytest.raises(ValueError, match="open_channel_prompts"):
        validate_config(cfg)
