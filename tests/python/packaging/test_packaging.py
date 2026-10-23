# Packaging tests. Slow, isolated, never overwrite dist/astral-remote.exe.
# Skipped unless ASTRAL_RUN_PACKAGING=1. Run explicitly after unit green.

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_RUN_PACKAGING") != "1",
    reason="packaging runs only with ASTRAL_RUN_PACKAGING=1",
)


def test_pyinstaller_onefile_smoke(tmp_path):
    import astral_remote.builder.build as build_mod

    monkeypatch_exe = tmp_path / build_mod.DEFAULT_EXE_NAME
    build_mod.DIST_DIR = tmp_path
    build_mod.OUTPUT_EXE = monkeypatch_exe
    build_mod.BUILD_TMP = tmp_path / "build-tmp"
    result = build_mod.build_agent()
    assert monkeypatch_exe.exists()
    payload = monkeypatch_exe.read_bytes()
    assert b"TOKEN =" not in payload
    assert b"botToken" not in payload
    assert "AstralRemote-Agent" in result["version_line"]
    from astral_remote.builder.subsystem import read_subsystem
    from astral_remote.builder.subsystem import IMAGE_SUBSYSTEM_WINDOWS_GUI

    assert read_subsystem(monkeypatch_exe.read_bytes()) == IMAGE_SUBSYSTEM_WINDOWS_GUI
