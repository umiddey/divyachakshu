from pathlib import Path

from oi_agent.opencode.native import environment_for


def test_mise_trust_list_passes_through_but_secrets_do_not(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", "/srv/work")
    monkeypatch.setenv("ZAI_API_KEY", "should-not-leak")
    env = environment_for(tmp_path, tmp_path / "data")
    assert env["MISE_TRUSTED_CONFIG_PATHS"] == "/srv/work"
    assert "ZAI_API_KEY" not in env
    assert env["HOME"] == str(tmp_path / "home")
