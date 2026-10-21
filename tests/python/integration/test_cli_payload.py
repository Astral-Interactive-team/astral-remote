# Integration tests: payload codegen roundtrip and CLI smoke.
# No network. No PyInstaller. No production artifact writes.

import os
import subprocess
import sys
from pathlib import Path

from astral_remote.builder.payload_codegen import render_payload_module
from astral_remote.shared.config import default_runtime_config
from tests.python.unit.test_payload import TOKEN_A

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_codegen_module_loads_through_import(tmp_path, monkeypatch):
    source = render_payload_module(TOKEN_A, default_runtime_config())
    target = tmp_path / "_embedded_payload.py"
    target.write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    import importlib

    module = importlib.import_module("_embedded_payload")
    assert module.TOKEN == TOKEN_A
    assert module.CONFIG["startupName"] == "AstralRemote"


def test_cli_version_smoke():
    proc = subprocess.run(
        [sys.executable, "-m", "astral_remote", "--version"],
        capture_output=True, text=True, timeout=60, shell=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert proc.returncode == 0
    assert "astral-remote" in proc.stdout.lower()
