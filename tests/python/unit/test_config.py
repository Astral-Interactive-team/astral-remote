# Unit tests for configuration schema and validation.

from astral_remote.shared.config import (
    default_runtime_config,
    require_valid_config,
    validate_config,
)
from astral_remote.shared.errors import ConfigError
import pytest


def test_defaults_pass_with_zero_user_input():
    assert validate_config(default_runtime_config()) == []


def test_runtime_config_carries_self_install_flags():
    defaults = default_runtime_config()
    assert defaults["installToAppData"] is True
    assert defaults["enableAutostart"] is True
    cfg = default_runtime_config(install_to_appdata=False, enable_autostart=False)
    assert cfg["installToAppData"] is False
    assert cfg["enableAutostart"] is False
    assert validate_config(cfg) == []


def test_rejects_non_boolean_self_install_flags():
    for key in ("installToAppData", "enableAutostart"):
        cfg = dict(default_runtime_config())
        cfg[key] = "yes"
        assert any(key in problem for problem in validate_config(cfg))


def test_runtime_config_carries_startup_name():
    assert default_runtime_config()["startupName"] == "AstralRemote"
    assert default_runtime_config(startup_name="MyAgent")["startupName"] == "MyAgent"
    blank = dict(default_runtime_config())
    blank["startupName"] = "  "
    assert any("startupName" in problem for problem in validate_config(blank))


def test_runtime_config_has_no_command_or_permission_keys():
    defaults = default_runtime_config()
    assert "enabledCommands" not in defaults
    assert "requiredPermission" not in defaults


def test_accepts_good_config():
    assert validate_config({"agentVersion": "0.1.0", "startupName": "AstralRemote"}) == []


def test_rejects_empty_version():
    problems = validate_config({"agentVersion": ""})
    assert any("agentVersion" in p for p in problems)


def test_rejects_removed_ids():
    for legacy in ("guildId", "applicationId", "authorizedUserIds", "allowedApps"):
        cfg = dict(default_runtime_config())
        cfg[legacy] = "x"
        assert any(legacy in p for p in validate_config(cfg))


def test_rejects_embedded_token_fields():
    for key in ("botToken", "token"):
        cfg = dict(default_runtime_config())
        cfg[key] = "x"
        assert any("token" in p.lower() for p in validate_config(cfg))


def test_require_valid_config_throws_config_error():
    with pytest.raises(ConfigError):
        require_valid_config({"agentVersion": ""})
    assert require_valid_config(default_runtime_config()) is not None
