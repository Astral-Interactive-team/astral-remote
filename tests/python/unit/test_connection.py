# Unit tests for login backoff. Mirrors tests/build-verify.test.js login sections.

import asyncio

from astral_remote.agent.connection import backoff_delay, is_fatal_auth_error, login_with_backoff


class _Err(Exception):
    def __init__(self, message="", code=None):
        super().__init__(message)
        self.code = code


def test_backoff_grows_then_caps():
    assert backoff_delay(0) == 1000
    assert backoff_delay(1) == 2000
    assert backoff_delay(10) == 60000


def test_returns_first_success():
    calls = []

    async def ok():
        calls.append(1)
        return "ok"

    assert asyncio.run(login_with_backoff(ok, 3, sleep=_no_sleep)) == "ok"
    assert len(calls) == 1


async def _no_sleep(_delay):
    return None


def test_throws_last_error_after_exhaustion():
    calls = []

    async def fail():
        calls.append(1)
        raise RuntimeError(f"fail-{len(calls)}")

    try:
        asyncio.run(login_with_backoff(fail, 2, sleep=_no_sleep))
        raise AssertionError("should have thrown")
    except RuntimeError as err:
        assert "fail-2" in str(err)
    assert len(calls) == 2


def test_fatal_auth_no_retry():
    calls = []

    async def fail():
        calls.append(1)
        raise _Err("An invalid token was provided.")

    try:
        asyncio.run(login_with_backoff(fail, 6, sleep=_no_sleep))
        raise AssertionError("should have thrown")
    except _Err:
        pass
    assert len(calls) == 1


def test_is_fatal_matches_invalid_tokens():
    assert is_fatal_auth_error(_Err("An invalid token was provided.")) is True
    assert is_fatal_auth_error(_Err("x", code="TokenInvalid")) is True
    assert is_fatal_auth_error(RuntimeError("getaddrinfo ENOTFOUND")) is False
    assert is_fatal_auth_error(None) is False
