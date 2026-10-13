"""A bare `opencode` must resolve to a candidate that passes the v2 version check."""

import os
from pathlib import Path

import pytest

from oi_agent.opencode import bootstrap
from oi_agent.opencode.paths import resolve_opencode_paths


def _fake(path: Path, version: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\necho 'opencode {version}'\n")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def _clear_cache():
    bootstrap._runtime_binaries.clear()
    yield
    bootstrap._runtime_binaries.clear()


def test_skips_wrong_major_launcher_on_path(tmp_path, monkeypatch):
    shim = _fake(tmp_path / "shim" / "opencode", "1.19.0")
    official = _fake(tmp_path / "home" / ".opencode" / "bin" / "opencode", "v2.0.5")
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", official.parent)
    paths = resolve_opencode_paths(tmp_path / "home")
    assert bootstrap.resolve_runtime_binary("opencode", paths) == str(official.resolve())


def test_no_supported_candidate_names_rejections(tmp_path, monkeypatch):
    shim = _fake(tmp_path / "shim" / "opencode", "1.19.0")
    monkeypatch.setenv("PATH", str(shim.parent))
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", tmp_path / "none")
    paths = resolve_opencode_paths(tmp_path / "home")
    with pytest.raises(bootstrap.OpenCodeBootstrapError, match="1.19.0"):
        bootstrap.resolve_runtime_binary("opencode", paths)
