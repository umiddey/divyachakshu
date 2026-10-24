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


def test_native_candidate_wins_over_earlier_script(tmp_path, monkeypatch):
    script = _fake(tmp_path / "bin1" / "opencode", "v2.0.5")
    native = _fake(tmp_path / "bin2" / "opencode", "v2.0.5")
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}{native.parent}")
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", tmp_path / "none")
    monkeypatch.setattr(
        bootstrap, "_is_native_binary", lambda p: Path(p).resolve() == native.resolve()
    )
    paths = resolve_opencode_paths(tmp_path / "home")
    assert bootstrap.resolve_runtime_binary("opencode", paths) == str(native.resolve())


def test_script_candidate_still_chosen_when_no_native(tmp_path, monkeypatch):
    script = _fake(tmp_path / "bin1" / "opencode", "v2.0.5")
    monkeypatch.setenv("PATH", str(script.parent))
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", tmp_path / "none")
    paths = resolve_opencode_paths(tmp_path / "home")
    assert bootstrap.resolve_runtime_binary("opencode", paths) == str(script.resolve())


def test_failing_native_falls_back_to_script(tmp_path, monkeypatch):
    bad_native = _fake(tmp_path / "bin1" / "opencode", "1.19.0")
    script = _fake(tmp_path / "bin2" / "opencode", "v2.0.5")
    monkeypatch.setenv("PATH", f"{bad_native.parent}{os.pathsep}{script.parent}")
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", tmp_path / "none")
    monkeypatch.setattr(
        bootstrap,
        "_is_native_binary",
        lambda p: Path(p).resolve() == bad_native.resolve(),
    )
    paths = resolve_opencode_paths(tmp_path / "home")
    assert bootstrap.resolve_runtime_binary("opencode", paths) == str(script.resolve())


def test_runtime_candidate_order_keeps_path_order_within_groups(tmp_path, monkeypatch):
    script_a = _fake(tmp_path / "a" / "opencode", "v2.0.5")
    native_b = _fake(tmp_path / "b" / "opencode", "v2.0.5")
    script_c = _fake(tmp_path / "c" / "opencode", "v2.0.5")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [str(script_a.parent), str(native_b.parent), str(script_c.parent)]
        ),
    )
    monkeypatch.setattr(bootstrap, "OFFICIAL_INSTALL_DIR", tmp_path / "none")
    monkeypatch.setattr(
        bootstrap, "_is_native_binary", lambda p: Path(p).resolve() == native_b.resolve()
    )
    ordered = bootstrap._ordered_runtime_candidates("opencode")
    assert ordered == [native_b.resolve(), script_a.resolve(), script_c.resolve()]


def test_runtime_native_magic_classification(tmp_path):
    elf = tmp_path / "elf"
    elf.write_bytes(b"\x7fELF" + b"\x02\x01\x01\x00" + b"\x00" * 8)
    macho = tmp_path / "macho"
    macho.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 4)
    fat = tmp_path / "fat"
    fat.write_bytes(b"\xca\xfe\xba\xbe" + b"\x00" * 4)
    script = tmp_path / "script"
    script.write_text("#!/bin/sh\ntrue\n")
    text = tmp_path / "text"
    text.write_text("plain")
    assert bootstrap._is_native_binary(elf)
    assert bootstrap._is_native_binary(macho)
    assert bootstrap._is_native_binary(fat)
    assert not bootstrap._is_native_binary(script)
    assert not bootstrap._is_native_binary(text)
    assert not bootstrap._is_native_binary(tmp_path / "missing")
