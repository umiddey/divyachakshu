"""Tests for OpenCode discovery, version, auth, and model bootstrap."""

import sqlite3
import subprocess
import time
from types import SimpleNamespace

import pytest

from oi_agent.opencode import bootstrap
from oi_agent.opencode.paths import resolve_opencode_paths


def test_installed_binary_version_is_verified():
    """The local compatible executable reports a parsed semantic version."""
    version = bootstrap.check_version("opencode", resolve_opencode_paths())
    assert version.binary.endswith(("opencode", "opencode.exe"))
    assert len(version.version) == 3
    assert version.version >= bootstrap.MIN_OPENCODE_VERSION


def test_unsupported_major_versions_rejected(monkeypatch):
    """v1 and any unknown future major are rejected, never run under ignored policy."""
    for raw in ("opencode 1.10.0\n", "opencode 1.18.0\n", "opencode 3.1.4\n"):
        monkeypatch.setattr(
            bootstrap.subprocess,
            "run",
            lambda *args, raw=raw, **kwargs: SimpleNamespace(
                returncode=0, stdout=raw
            ),
        )
        with pytest.raises(
            bootstrap.OpenCodeBootstrapError,
            match="not a supported native v2 release",
        ):
            bootstrap.check_version("opencode", resolve_opencode_paths())


def test_minimum_v2_version_accepted_and_enforced(monkeypatch):
    """A current v2 release parses; a pre-minimum v2 release is too old."""
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="opencode 2.0.5\n"),
    )
    assert bootstrap.check_version("opencode", resolve_opencode_paths()).version == (2, 0, 5)

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="opencode 1.99.9\n"),
    )
    with pytest.raises(bootstrap.OpenCodeBootstrapError, match="not a supported native v2"):
        bootstrap.check_version("opencode", resolve_opencode_paths())


def test_missing_binary_has_actionable_diagnostic(tmp_path):
    """Missing executables identify the official installation documentation."""
    with pytest.raises(bootstrap.OpenCodeBootstrapError, match="opencode.ai/docs"):
        bootstrap.resolve_binary(str(tmp_path / "missing"))


def test_model_listing_returns_exact_ids_without_raw_output(monkeypatch):
    """Model parsing extracts provider/model IDs and deduplicates them."""
    monkeypatch.setattr(
        bootstrap,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Provider\nopenai/gpt-4o-mini\nopenai/gpt-4o-mini\n",
        ),
    )
    assert bootstrap.list_models("opencode", resolve_opencode_paths()) == [
        "openai/gpt-4o-mini"
    ]


def test_auth_check_reflects_credential_store_not_integration_listing(
        tmp_path, monkeypatch, seed_credential_db):
    """The auth boolean comes from the isolated credential store.

    v2 `auth list` reports INTEGRATIONS, not provider credentials: on a
    working install (2.0.5) it prints "No authenticated integrations" even
    though runs authenticate fine from the credential table. The string check
    on that output returned True by accident, so the probe must read the
    credential table instead — read-only, and never the secret value column.
    """
    paths = resolve_opencode_paths(tmp_path)
    connections = []
    real_connect = sqlite3.connect

    def tracking_connect(target, *args, **kwargs):
        connections.append((str(target), kwargs))
        return real_connect(target, *args, **kwargs)

    # raising=False: on the unfixed module bootstrap has no sqlite3 import at
    # all; the test then fails on the accidental-True string check below.
    monkeypatch.setattr(bootstrap, "sqlite3", sqlite3, raising=False)
    monkeypatch.setattr(bootstrap.sqlite3, "connect", tracking_connect)
    # The proven v2 wire shape must not decide the boolean on its own.
    monkeypatch.setattr(
        bootstrap,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="No authenticated integrations\n"
        ),
    )

    assert bootstrap.authenticated(paths) is False
    seed_credential_db(paths.data_home, "zai-coding-plan")
    assert bootstrap.authenticated(paths) is True

    assert connections, "credential probe must read the isolated native store"
    # The seeding connection is a plain path; the probe itself must be read-only.
    assert any("mode=ro" in target for target, _kwargs in connections)


def test_inactive_credential_rows_are_not_authenticated(tmp_path, seed_credential_db):
    """Explicitly deactivated rows (active = 0) never pass.

    A zero row is a deliberate deactivation, so an inactive-only store must
    not read as authenticated — otherwise doctor passes and the following
    run cannot use that credential.
    """
    paths = resolve_opencode_paths(tmp_path)
    seed_credential_db(paths.data_home, "zai-coding-plan", active=0)
    assert bootstrap.authenticated(paths) is False


def test_null_active_credential_rows_authenticate(tmp_path, seed_credential_db):
    """Untouched rows (active IS NULL) authenticate.

    The real OI-isolated store carries `active = NULL` rows that provably
    authenticate live runs, so NULL must not be mistaken for deactivated;
    only an explicit 0 excludes a row.
    """
    paths = resolve_opencode_paths(tmp_path)
    seed_credential_db(paths.data_home, "zai-coding-plan", active=None)
    assert bootstrap.authenticated(paths) is True


def test_mixed_active_and_inactive_credentials_authenticate(
        tmp_path, seed_credential_db):
    """One live row is enough even when other rows are deactivated."""
    paths = resolve_opencode_paths(tmp_path)
    seed_credential_db(paths.data_home, "zai-coding-plan", active=0)
    seed_credential_db(paths.data_home, "zai", active=1)
    assert bootstrap.authenticated(paths) is True


def test_missing_credential_table_fails_closed(tmp_path):
    """A native database without a credential table reads as unauthenticated."""
    paths = resolve_opencode_paths(tmp_path)
    db = paths.data_home / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    assert bootstrap.authenticated(paths) is False


def test_corrupt_credential_database_fails_closed(tmp_path):
    """A garbage database file reads as unauthenticated, never crashes."""
    paths = resolve_opencode_paths(tmp_path)
    db = paths.data_home / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is not sqlite")
    assert bootstrap.authenticated(paths) is False


def test_model_status_trusts_nonempty_listing(tmp_path, monkeypatch):
    """A non-empty listing is authoritative: presence verifies, absence fails."""
    paths = resolve_opencode_paths(tmp_path)
    model = "zai-coding-plan/glm-5.3-flash"
    monkeypatch.setattr(
        bootstrap, "list_models", lambda *args, **kwargs: [model, "openai/gpt-4o"]
    )
    listed = bootstrap.model_status("opencode", paths, model)
    assert (listed.available, listed.verified) == (True, True)
    missing = bootstrap.model_status("opencode", paths, "openai/o3-mini")
    assert (missing.available, missing.verified) == (False, True)


def test_model_status_empty_listing_with_credential_is_unverified(
        tmp_path, monkeypatch, seed_credential_db):
    """The empty listing carries no model-id signal, so the fallback is unverified.

    `models --standalone` exits 0 with EMPTY output both on working installs
    and with zero credentials, so the credential fallback must be reported as
    available-but-unverified: it proves the provider is credentialed, not that
    the exact model id exists.
    """
    paths = resolve_opencode_paths(tmp_path)
    seed_credential_db(paths.data_home, "zai-coding-plan", active=1)
    monkeypatch.setattr(bootstrap, "list_models", lambda *args, **kwargs: [])

    real = bootstrap.model_status("opencode", paths, "zai-coding-plan/glm-5.3-flash")
    assert (real.available, real.verified) == (True, False)
    # A nonexistent id under the credentialed provider gets the SAME outcome:
    # exactly why this state must be surfaced as unverified, not as a pass.
    bogus = bootstrap.model_status("opencode", paths, "zai-coding-plan/does-not-exist-9")
    assert (bogus.available, bogus.verified) == (True, False)


def test_model_status_empty_listing_without_live_credential_fails(
        tmp_path, monkeypatch, seed_credential_db):
    """Without a live row for the provider the model is genuinely unavailable."""
    paths = resolve_opencode_paths(tmp_path)
    monkeypatch.setattr(bootstrap, "list_models", lambda *args, **kwargs: [])
    none = bootstrap.model_status("opencode", paths, "zai-coding-plan/glm-5.3-flash")
    assert (none.available, none.verified) == (False, False)

    seed_credential_db(paths.data_home, "zai-coding-plan", active=0)
    inactive = bootstrap.model_status(
        "opencode", paths, "zai-coding-plan/glm-5.3-flash"
    )
    assert (inactive.available, inactive.verified) == (False, False)


def test_bootstrap_probe_subcommands_pass_standalone(tmp_path, monkeypatch):
    """Shelled bootstrap subcommands run --standalone with a bounded timeout.

    Without --standalone every v2 CLI subcommand tries to connect to (or
    start) the managed service on 127.0.0.1:49374; when the user's own
    `opencode serve --service` holds that port, the probe loops forever and
    `subprocess.run(timeout=600)` hangs the doctor. A bounded probe timeout
    turns that hang into a reported failure.
    """
    paths = resolve_opencode_paths(tmp_path)
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((list(cmd), kwargs))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)

    bootstrap.list_models("opencode", paths)
    models_cmd, models_kwargs = seen[-1]
    assert models_cmd[1:2] == ["models"]
    assert "--standalone" in models_cmd
    assert 0 < models_kwargs["timeout"] <= 60

    with pytest.raises(bootstrap.OpenCodeBootstrapError):
        bootstrap.authenticate("opencode", paths)  # fake login verifies nothing
    login_cmd, _login_kwargs = seen[-1]
    assert login_cmd[1:3] == ["auth", "login"]
    assert "--standalone" in login_cmd


def test_probe_timeout_becomes_reported_failure(tmp_path, monkeypatch):
    """A wedged probe is reported, never hung and never 'authenticated'."""
    paths = resolve_opencode_paths(tmp_path)

    def wedged(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    monkeypatch.setattr(bootstrap.subprocess, "run", wedged)
    with pytest.raises(bootstrap.OpenCodeBootstrapError, match="did not finish"):
        bootstrap.list_models("opencode", paths)
    assert bootstrap.authenticated(paths) is False


def test_wedged_process_is_killed_by_probe_timeout(tmp_path):
    """A really hanging CLI is killed and the probe raises within the budget."""
    wedged = tmp_path / "wedged-opencode"
    wedged.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    wedged.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        bootstrap._run(str(wedged), ["models"], resolve_opencode_paths(tmp_path),
                       timeout=0.5)
    assert time.monotonic() - started < 10


def test_paths_are_isolated_and_shared(tmp_path):
    """The resolver sets private HOME/XDG roots and suppresses inherited config."""
    paths = resolve_opencode_paths(tmp_path)
    env = paths.environment({
        "HOME": "/operator",
        "XDG_CONFIG_HOME": "/personal",
        "OPENCODE_CONFIG": "/personal/opencode.json",
        "OPENCODE_CONFIG_DIR": "/personal/opencode",
    })
    assert env["HOME"].startswith(str(tmp_path))
    assert env["XDG_CONFIG_HOME"].startswith(str(tmp_path))
    assert "OPENCODE_CONFIG" not in env
    assert "OPENCODE_CONFIG_DIR" not in env
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"


def test_environment_strictly_excludes_parent_secrets(monkeypatch, tmp_path):
    """Parent process secrets are never inherited by the OpenCode subprocess."""
    monkeypatch.setenv("OI_DISCORD_TOKEN", "discord-token-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "anthropic-token-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("DATABASE_PASSWORD", "db-pass")
    monkeypatch.setenv("MY_PRIVATE_KEY", "key-data")
    monkeypatch.setenv("CUSTOM_CREDENTIAL", "cred-data")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    paths = resolve_opencode_paths(tmp_path)
    env = paths.environment()  # base=None -> reads os.environ through allowlist

    assert "OI_DISCORD_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_PASSWORD" not in env
    assert "MY_PRIVATE_KEY" not in env
    assert "CUSTOM_CREDENTIAL" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"


def test_install_command_selection(monkeypatch):
    """Install command selects official package managers in order."""
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: "/bin/" + cmd if cmd == "npm" else None)
    assert bootstrap.install_command() == ["npm", "install", "--global", "opencode-ai"]

    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: "/bin/" + cmd if cmd == "brew" else None)
    assert bootstrap.install_command() == ["brew", "install", "anomalyco/tap/opencode"]

    monkeypatch.setattr(bootstrap.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda cmd: "/bin/" + cmd if cmd == "mise" else None)
    assert bootstrap.install_command() == ["mise", "use", "--global", "npm:opencode-ai"]
