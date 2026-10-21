# Unit tests for Windows host facts shared by the managed #info channel.
# The full #info dashboard lives in info_report.py and has its own test
# module.

from astral_remote.agent.windows.info import (
    get_hostname,
    get_local_ip,
    get_machine_id,
    get_uptime_seconds,
)


def test_hostname_is_a_non_empty_string():
    hostname = get_hostname()
    assert isinstance(hostname, str) and len(hostname) > 0


def test_local_ip_is_a_plain_string():
    assert isinstance(get_local_ip(), str) and len(get_local_ip()) > 0


def test_uptime_helper_is_monotonic_and_non_negative():
    assert isinstance(get_uptime_seconds(), int)
    assert get_uptime_seconds() >= 0


def test_machine_id_short_and_stable():
    first = get_machine_id()
    assert get_machine_id() == first
    assert first == "unknown-id" or (len(first) == 8 and all(c in "0123456789abcdef" for c in first))
