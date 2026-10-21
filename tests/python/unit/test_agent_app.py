# Unit tests for early argument dispatch: --version never touches the
# payload, --self-test maps missing/invalid payloads to exit codes.

import sys

from astral_remote.agent.app import (
    EXIT_BAD_CONFIG,
    EXIT_BAD_TOKEN,
    EXIT_NO_PAYLOAD,
    EXIT_OK,
    handle_self_test,
    handle_version,
)


def test_version_flag_no_payload_import(capsys, monkeypatch):
    monkeypatch.setitem(sys.modules, "astral_remote._embedded_payload", None)
    assert handle_version(["--version"]) is True
    assert handle_version(["-v"]) is True
    assert handle_version([]) is False
    out = capsys.readouterr().out
    assert "AstralRemote-Agent" in out


def test_self_test_missing_payload(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "astral_remote._embedded_payload" or name.endswith("._embedded_payload"):
            raise ImportError("blocked")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert handle_self_test(["--self-test"]) == EXIT_NO_PAYLOAD


def test_self_test_invalid_token_shape(monkeypatch):
    import types

    module = types.ModuleType("astral_remote._embedded_payload")
    module.PAYLOAD_V = 2
    module.TOKEN = "junk"
    from astral_remote.shared.config import default_runtime_config

    module.CONFIG = default_runtime_config()
    monkeypatch.setitem(sys.modules, "astral_remote._embedded_payload", module)
    assert handle_self_test(["--self-test"]) == EXIT_BAD_TOKEN


def test_self_test_ok_with_valid_payload(monkeypatch, tmp_path):
    import types

    from tests.python.unit.test_payload import TOKEN_A

    module = types.ModuleType("astral_remote._embedded_payload")
    module.PAYLOAD_V = 2
    module.TOKEN = TOKEN_A
    from astral_remote.shared.config import default_runtime_config

    module.CONFIG = default_runtime_config()
    monkeypatch.setitem(sys.modules, "astral_remote._embedded_payload", module)
    assert handle_self_test(["--self-test"]) == EXIT_OK


def test_self_test_bad_config_maps_to_config_code(monkeypatch):
    import types

    from tests.python.unit.test_payload import TOKEN_A

    module = types.ModuleType("astral_remote._embedded_payload")
    module.PAYLOAD_V = 2
    module.TOKEN = TOKEN_A
    module.CONFIG = {"agentVersion": ""}
    monkeypatch.setitem(sys.modules, "astral_remote._embedded_payload", module)
    assert handle_self_test(["--self-test"]) == EXIT_BAD_CONFIG
    assert EXIT_OK == 0
