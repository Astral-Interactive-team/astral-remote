# Unit tests for menu parsing and navigation. Mirrors tests/dev-menu.test.js
# parser sections. Rendering is covered in test_views.py.

from astral_remote.cli.menu import parse_dev_choice, parse_home_choice, parse_remote_choice


def test_home_maps_links():
    assert parse_home_choice("01") == "github"
    assert parse_home_choice("1") == "github"
    assert parse_home_choice("02") == "telegram"
    assert parse_home_choice("2") == "telegram"
    assert parse_home_choice("03") == "discord"
    assert parse_home_choice("3") == "discord"


def test_home_nav_and_exit():
    assert parse_home_choice("n") == "next"
    assert parse_home_choice("NEXT") == "next"
    for raw in ("q", "Q", "quit", "exit", ""):
        assert parse_home_choice(raw) == "exit"
    assert parse_home_choice(None) == "exit"


def test_home_rejects_invalid():
    for raw in ("4", "asdf", "11", "0", "00", "b", "back"):
        assert parse_home_choice(raw) is None


def test_remote_maps_operations():
    assert parse_remote_choice("01") == "generate"
    assert parse_remote_choice("1") == "generate"
    assert parse_remote_choice("02") == "start"
    assert parse_remote_choice("b") == "back"
    assert parse_remote_choice("BACK") == "back"
    assert parse_remote_choice("q") == "exit"


def test_remote_rejects_invalid():
    for raw in ("3", "asdf", "n", "next", ""):
        assert parse_remote_choice(raw) is None
    assert parse_remote_choice(None) is None


def test_compat_delegates():
    assert parse_dev_choice("01") == "generate"
    assert parse_dev_choice("02") == "start"
    assert parse_dev_choice("b") == "back"
    assert parse_dev_choice("q") == "exit"
    assert parse_dev_choice("99") is None


def test_navigation_never_triggers_operations():
    assert parse_home_choice("N") not in ("generate", "start")
    assert parse_remote_choice("B") not in ("generate", "start")
