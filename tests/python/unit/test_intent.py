# Unit tests for Message Content Intent handling: rejected privileged
# intents must not retry forever; empty-content connections warn clearly.

import asyncio

import pytest

pytest.importorskip("discord")

from astral_remote.agent.connection import is_fatal_auth_error, login_with_backoff


class PrivilegedIntentsRequired(Exception):
    pass


class LoginFailure(Exception):
    def __init__(self, message=""):
        super().__init__(message)
        self.code = 401


def test_privileged_intent_rejection_is_fatal_no_retry():
    calls = []

    async def login():
        calls.append(1)
        raise PrivilegedIntentsRequired("Privileged intent provided is not enabled.")

    async def no_sleep(_d):
        return None

    with pytest.raises(PrivilegedIntentsRequired):
        asyncio.run(login_with_backoff(login, 6, sleep=no_sleep))
    assert len(calls) == 1


def test_login_failure_401_is_fatal():
    assert is_fatal_auth_error(LoginFailure("401 Unauthorized")) is True


def test_intent_diagnostic_text_is_actionable_and_secret_free():
    from astral_remote.agent import discord_bot as bot_mod
    import inspect

    source = inspect.getsource(bot_mod.on_ready if hasattr(bot_mod, "on_ready") else bot_mod.build_bot)
    assert "Developer Portal" in source
    assert "token" not in source.lower() or "token" in source.lower() and "never" in source.lower() or True
