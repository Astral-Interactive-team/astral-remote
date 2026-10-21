# Unit tests for secret redaction. Mirrors tests/redaction.test.js.

from astral_remote.shared.logging_utils import redact

TOKEN = "QUJDREVGR0hJSktMTU5PUFFS.VEVTVC1BLVRPS0VO.MTIzNDU2Nzg5MEFCQ0RFRkdI"


def test_masks_token_named_keys():
    assert redact({"botToken": TOKEN, "ok": "yes"}) == {"botToken": "[REDACTED]", "ok": "yes"}


def test_masks_long_secret_runs():
    assert redact("prefix " + "x" * 30 + " suffix") == "prefix [REDACTED] suffix"


def test_leaves_short_ordinary_text_alone():
    assert redact("hello world") == "hello world"


def test_redacts_nested_arrays_and_objects():
    assert redact([{"secret": "s"}, ["a", TOKEN]]) == [{"secret": "[REDACTED]"}, ["a", "[REDACTED]"]]


def test_masks_bot_token_shapes_inside_text():
    assert TOKEN not in redact(f"login with {TOKEN} now")
    assert "[REDACTED]" in redact(f"login with {TOKEN} now")
