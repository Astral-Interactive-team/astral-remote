# Unit tests for the persistent, non-secret installation identity store.
# Covers ID generation/persistence, guild-category memory, tolerant
# loading, and env-based path resolution. No filesystem outside tmp.

import json

from astral_remote.agent import identity
from astral_remote.agent.identity import StateStore, load_state, save_state, state_file


def test_state_file_uses_state_dir(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert state_file(env) == tmp_path / "AstralRemote" / identity.STATE_FILENAME
    assert state_file({}) is None


def test_legacy_state_is_adopted_into_the_new_directory(tmp_path):
    legacy_directory = tmp_path / "AstralRelay"
    legacy_directory.mkdir()
    legacy = legacy_directory / identity.STATE_FILENAME
    legacy.write_text(json.dumps({
        "version": 1,
        "installationId": "legacy-id",
        "guilds": {"1": {"categoryId": 11, "name": "HOST", "infoChannelId": 5,
                         "infoMessages": [7, 8]}},
    }), encoding="utf-8")

    env = {"LOCALAPPDATA": str(tmp_path)}
    state = load_state(env=env)
    assert state["installationId"] == "legacy-id"
    assert state["guilds"]["1"]["infoMessages"] == [7, 8]
    # The pre-rename file is copied forward and left in place.
    adopted = tmp_path / "AstralRemote" / identity.STATE_FILENAME
    assert adopted.is_file()
    assert legacy.is_file()
    assert load_state(env=env)["guilds"]["1"]["categoryId"] == 11


def test_existing_new_state_is_not_overwritten_by_the_legacy_file(tmp_path):
    legacy_directory = tmp_path / "AstralRelay"
    legacy_directory.mkdir()
    (legacy_directory / identity.STATE_FILENAME).write_text(
        json.dumps({"installationId": "legacy-id"}), encoding="utf-8")
    new_directory = tmp_path / "AstralRemote"
    new_directory.mkdir()
    (new_directory / identity.STATE_FILENAME).write_text(
        json.dumps({"installationId": "new-id"}), encoding="utf-8")

    state = load_state(env={"LOCALAPPDATA": str(tmp_path)})
    assert state["installationId"] == "new-id"


def test_explicit_state_path_never_adopts_the_legacy_file(tmp_path):
    legacy_directory = tmp_path / "AstralRelay"
    legacy_directory.mkdir()
    (legacy_directory / identity.STATE_FILENAME).write_text(
        json.dumps({"installationId": "legacy-id"}), encoding="utf-8")

    explicit = tmp_path / "explicit.json"
    assert load_state(explicit, env={"LOCALAPPDATA": str(tmp_path)})["installationId"] == ""
    assert not (tmp_path / "AstralRemote").exists()


def test_installation_id_generated_and_persisted(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    first = store.installation_id
    assert first and len(first) >= 16

    reopened = StateStore(path)
    assert reopened.installation_id == first
    assert json.loads(path.read_text(encoding="utf-8"))["installationId"] == first


def test_installation_id_generated_only_once_per_store(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    assert store.installation_id == store.installation_id


def test_guild_category_memory_round_trip(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember(12345, 999, "HOST [abcdef]")

    reopened = StateStore(path)
    assert reopened.category_id(12345) == 999
    assert reopened.category_id(99999) is None


def test_info_channel_and_message_memory_round_trip(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember(12345, 999, "HOST [abcdef]")
    store.remember_info(12345, channel_id=555, message_id=777)
    store.remember_main(12345, 556)

    reopened = StateStore(path)
    assert reopened.category_id(12345) == 999
    assert reopened.info_channel_id(12345) == 555
    assert reopened.main_channel_id(12345) == 556
    assert reopened.info_message_id(12345) == 777
    assert reopened.info_channel_id(99999) is None
    assert reopened.main_channel_id(99999) is None
    # Remembering the category again must not drop the channel IDs.
    reopened.remember(12345, 999, "HOST [abcdef]")
    assert StateStore(path).info_message_id(12345) == 777
    assert StateStore(path).main_channel_id(12345) == 556


def test_control_category_and_channel_memory_round_trip(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember_control(12345, category_id=800, main_channel_id=801, logs_channel_id=802)

    reopened = StateStore(path)
    assert reopened.control_category_id(12345) == 800
    assert reopened.control_main_channel_id(12345) == 801
    assert reopened.control_logs_channel_id(12345) == 802
    assert reopened.control_category_id(99999) is None
    # The host category can be remembered later without dropping control IDs.
    reopened.remember(12345, 999, "HOST [abcdef]")
    reopened.remember_control(12345, main_channel_id=803)
    saved = StateStore(path)
    assert saved.control_category_id(12345) == 800
    assert saved.control_main_channel_id(12345) == 803
    assert saved.control_logs_channel_id(12345) == 802


def test_control_state_survives_without_a_host_category(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember_control(12345, category_id=800)

    reopened = StateStore(path)
    assert reopened.control_category_id(12345) == 800
    assert reopened.category_id(12345) is None


def test_device_message_memory_round_trip(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember_control(12345, category_id=800, main_channel_id=801)
    store.remember_device_message(12345, 901)
    store.remember_heartbeat_message(12345, 902)

    reopened = StateStore(path)
    assert reopened.device_message_id(12345) == 901
    assert reopened.heartbeat_message_id(12345) == 902
    assert reopened.control_main_channel_id(12345) == 801
    assert reopened.device_message_id(99999) is None
    assert reopened.heartbeat_message_id(99999) is None

    reopened.remember_control(12345, main_channel_id=803)
    saved = StateStore(path)
    assert saved.device_message_id(12345) == 901
    assert saved.heartbeat_message_id(12345) == 902


def test_report_message_list_round_trips_and_drops_the_legacy_field(tmp_path):
    path = tmp_path / "identity.json"
    store = StateStore(path)
    store.remember(12345, 999, "HOST [abcdef]")
    assert store.info_messages(12345) == []

    store.remember_info_messages(12345, [777, 778, 779])
    reopened = StateStore(path)
    assert reopened.info_messages(12345) == [777, 778, 779]
    # The legacy single-ID field is dropped once a real list is stored.
    record = reopened._record(12345)
    assert "infoMessageId" not in record

    # A shorter report replaces the list rather than appending to it.
    reopened.remember_info_messages(12345, [777])
    assert StateStore(path).info_messages(12345) == [777]
    assert StateStore(path).info_messages(99999) == []


def test_report_message_list_reads_the_legacy_single_id(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "installationId": "abc",
        "guilds": {"1": {"categoryId": 7, "name": "n", "infoChannelId": 5, "infoMessageId": 42}},
    }), encoding="utf-8")
    store = StateStore(legacy)
    # An existing panel from an older build is refreshed in place, not duplicated.
    assert store.info_messages(1) == [42]
    store.remember_info_messages(1, [42, 43])
    assert StateStore(legacy).info_messages(1) == [42, 43]

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text(json.dumps({
        "installationId": "abc",
        "guilds": {"1": {"categoryId": 7, "name": "n", "infoMessages": "not-a-list", "infoMessageId": 42}},
    }), encoding="utf-8")
    # A corrupt list is ignored in favour of the legacy single ID, never a crash.
    assert StateStore(corrupt).info_messages(1) == [42]


def test_load_state_drops_malformed_info_ids(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({
        "installationId": "abc",
        "guilds": {"1": {"categoryId": 7, "name": "n", "infoChannelId": "x",
                         "mainChannelId": "y", "infoMessageId": 5}},
    }), encoding="utf-8")
    state = load_state(malformed)
    assert state["guilds"]["1"] == {"categoryId": 7, "name": "n", "infoMessageId": 5}


def test_load_state_drops_malformed_control_ids(tmp_path):
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({
        "installationId": "abc",
        "guilds": {"1": {"categoryId": 7, "name": "n",
                         "controlCategoryId": "x", "controlMainChannelId": True,
                         "controlLogsChannelId": 9,
                         "deviceMessageId": False, "heartbeatMessageId": 12}},
    }), encoding="utf-8")
    state = load_state(malformed)
    assert state["guilds"]["1"] == {
        "categoryId": 7, "name": "n", "controlLogsChannelId": 9,
        "heartbeatMessageId": 12,
    }


def test_load_state_keeps_a_control_only_record(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "installationId": "abc",
        "guilds": {"1": {"controlCategoryId": 800}},
    }), encoding="utf-8")
    assert load_state(path)["guilds"]["1"] == {"name": "", "controlCategoryId": 800}


def test_load_state_tolerates_missing_and_corrupt(tmp_path):
    missing = tmp_path / "nope.json"
    assert load_state(missing)["guilds"] == {}

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert load_state(corrupt)["installationId"] == ""

    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({
        "installationId": 5,
        "guilds": {"1": {"categoryId": "x"}, "2": {"categoryId": 7, "name": "ok"}},
    }), encoding="utf-8")
    state = load_state(malformed)
    assert state["installationId"] == ""
    assert state["guilds"] == {"2": {"categoryId": 7, "name": "ok"}}


def test_save_state_is_atomic_and_survives_reload(tmp_path):
    path = tmp_path / "identity.json"
    assert save_state({"installationId": "abc", "guilds": {"1": {"categoryId": 2, "name": "n"}}}, path)
    loaded = load_state(path)
    assert loaded["installationId"] == "abc"
    assert loaded["guilds"]["1"]["categoryId"] == 2
    # The temporary file is gone after a successful replace.
    assert list(tmp_path.glob(".identity.json.tmp-*")) == []


def test_save_state_reports_failure_without_raising(tmp_path):
    # A directory where the file should be makes the write fail cleanly.
    path = tmp_path / "as-dir"
    path.mkdir()
    assert save_state({"installationId": "abc", "guilds": {}}, path) is False
