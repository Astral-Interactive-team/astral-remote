# Unit tests for the sanitized diagnostic log and its summary.
# Writes land in tmp_path only; nothing outside tmp is touched.

from astral_remote.agent import error_log


def test_log_line_writes_a_timestamped_sanitized_entry(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    error_log.log_line("WARN", "duplicate host categories", env=env)
    text = (tmp_path / "AstralRemote" / error_log.LOG_FILENAME).read_text(encoding="utf-8")
    assert "[WARN] duplicate host categories" in text
    assert text.startswith("[")


def test_log_line_flattens_newlines_so_one_entry_stays_one_line(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    error_log.log_line("WARN", "first\nsecond", env=env)
    text = (tmp_path / "AstralRemote" / error_log.LOG_FILENAME).read_text(encoding="utf-8")
    assert "first second" in text
    assert len(text.strip().splitlines()) == 1


def test_summarize_log_counts_entries_without_echoing_text(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert error_log.summarize_log(env) == {
        "present": False, "entries": 0, "warnings": 0, "fatals": 0, "lastTimestamp": None,
    }

    error_log.log_line("WARN", "private-folder-name", env=env)
    error_log.log_line("WARN", "another", env=env)
    error_log.log_line("FATAL", "boom", env=env)

    summary = error_log.summarize_log(env)
    assert summary["present"] is True
    assert summary["entries"] == 3
    assert summary["warnings"] == 2
    assert summary["fatals"] == 1
    assert summary["lastTimestamp"]
    # Only counts and the timestamp leave the summary, never the message text.
    assert "private-folder-name" not in str(summary)


def test_log_helpers_never_raise_without_a_state_directory():
    error_log.log_line("WARN", "nowhere to write", env={})
    assert error_log.summarize_log(env={}) == {
        "present": False, "entries": 0, "warnings": 0, "fatals": 0, "lastTimestamp": None,
    }


def test_summarize_log_ignores_blank_lines(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    directory = tmp_path / "AstralRemote"
    directory.mkdir()
    (directory / error_log.LOG_FILENAME).write_text("\n[2026-01-01T00:00:00+00:00] [WARN] x\n\n", encoding="utf-8")
    summary = error_log.summarize_log(env)
    assert summary["entries"] == 1
    assert summary["lastTimestamp"] == "2026-01-01T00:00:00+00:00"
