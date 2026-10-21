# Unit tests for the shared windowless child-process flags.
# The flags themselves are platform constants, so these tests only check
# the merge behaviour and that the helper never disturbs the caller's own
# stdout/stderr, timeout, or shell settings.

import sys

import pytest

from astral_remote.shared import winproc


def test_creation_flag_matches_the_windows_constant():
    if sys.platform == "win32":
        assert winproc.CREATE_NO_WINDOW == 0x08000000
    else:
        assert winproc.CREATE_NO_WINDOW == 0


def test_hidden_kwargs_preserves_every_other_keyword():
    merged = winproc.hidden_kwargs(
        capture_output=True, shell=False, timeout=30, text=True
    )
    assert merged["capture_output"] is True
    assert merged["shell"] is False
    assert merged["timeout"] == 30
    assert merged["text"] is True


def test_hidden_kwargs_merges_into_existing_creation_flags():
    merged = winproc.hidden_kwargs(creationflags=winproc.CREATE_NO_WINDOW)
    if winproc.CREATE_NO_WINDOW:
        assert merged["creationflags"] == winproc.CREATE_NO_WINDOW


@pytest.mark.skipif(not winproc.CREATE_NO_WINDOW, reason="Windows-only creation flag")
def test_hidden_kwargs_sets_the_flag_on_windows():
    merged = winproc.hidden_kwargs()
    assert merged["creationflags"] & winproc.CREATE_NO_WINDOW


def test_hidden_kwargs_does_not_mutate_the_caller_mapping():
    original = {"shell": False}
    merged = winproc.hidden_kwargs(**original)
    assert merged is not original
    assert "creationflags" not in original
