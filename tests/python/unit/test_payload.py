# Unit tests for token shape, payload codegen, and embedded loading.

import sys

import pytest

from astral_remote.agent.payload import PAYLOAD_VERSION, is_plausible_bot_token
from astral_remote.builder.payload_codegen import render_payload_module
from astral_remote.shared.config import default_runtime_config
from astral_remote.shared.errors import BuildError

TOKEN_A = "QUJDREVGR0hJSktMTU5PUFFS.VEVTVC1BLVRPS0VO.MTIzNDU2Nzg5MEFCQ0RFRkdI"


def test_accepts_three_part_tokens():
    assert is_plausible_bot_token(TOKEN_A) is True
    assert is_plausible_bot_token("  " + TOKEN_A + "  ") is True


@pytest.mark.parametrize("bad", ["", "   ", "nodots", "a.b", "x.y", None, 42])
def test_rejects_malformed(bad):
    assert is_plausible_bot_token(bad) is False


def test_codegen_roundtrip():
    source = render_payload_module(TOKEN_A, default_runtime_config(prefix="pc!"))
    assert TOKEN_A not in source or "TOKEN" in source
    namespace: dict = {}
    exec(compile(source, "_embedded_payload", "exec"), namespace)
    assert namespace["PAYLOAD_V"] == PAYLOAD_VERSION
    assert namespace["TOKEN"] == TOKEN_A
    assert namespace["CONFIG"]["startupName"] == "AstralRemote"
    assert namespace["CONFIG"]["prefix"] == "pc!"


def test_codegen_rejects_bad_token_and_config():
    with pytest.raises(BuildError):
        render_payload_module("junk", default_runtime_config())
    bad = dict(default_runtime_config())
    bad["agentVersion"] = ""
    with pytest.raises(BuildError):
        render_payload_module(TOKEN_A, bad)
    bad_prefix = dict(default_runtime_config())
    bad_prefix["prefix"] = "two words"
    with pytest.raises(BuildError):
        render_payload_module(TOKEN_A, bad_prefix)


def test_load_missing_payload_raises(tmp_path, monkeypatch):
    from astral_remote.agent import payload as payload_mod

    monkeypatch.setitem(sys.modules, "astral_remote._embedded_payload", None)
    # Force ImportError by blocking the import machinery for this name.
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "astral_remote._embedded_payload" or name.endswith("._embedded_payload"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(FileNotFoundError):
        payload_mod.load_embedded_payload()
