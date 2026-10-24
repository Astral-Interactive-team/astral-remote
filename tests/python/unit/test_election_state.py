import pytest

from astral_remote.shared.security import (
    ElectionState,
    ElectionStateStore,
    SecurityStoreError,
    XorTestProtector,
)


def _store(path):
    return ElectionStateStore(
        path,
        XorTestProtector(b"election-test-key"),
    )


def test_initial_state_and_restart_persistence(tmp_path):
    path = tmp_path / "election.bin"
    store = _store(path)

    assert store.load() == ElectionState()

    assert store.grant_vote(1, "node-a") is True

    restarted = _store(path)
    assert restarted.load() == ElectionState(
        current_term=1,
        voted_for="node-a",
    )


def test_only_one_candidate_can_receive_a_vote_per_term(tmp_path):
    store = _store(tmp_path / "election.bin")

    assert store.grant_vote(1, "node-a") is True
    assert store.grant_vote(1, "node-a") is True
    assert store.grant_vote(1, "node-b") is False

    assert store.load().voted_for == "node-a"


def test_higher_term_resets_vote_and_stale_term_is_rejected(tmp_path):
    store = _store(tmp_path / "election.bin")

    assert store.grant_vote(1, "node-a") is True

    state = store.observe_term(2)
    assert state == ElectionState(current_term=2, voted_for=None)

    assert store.grant_vote(1, "node-b") is False
    assert store.grant_vote(2, "node-b") is True

    restarted = _store(tmp_path / "election.bin")
    assert restarted.load() == ElectionState(
        current_term=2,
        voted_for="node-b",
    )


def test_corrupt_state_fails_closed(tmp_path):
    path = tmp_path / "election.bin"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(SecurityStoreError):
        _store(path).load()


def test_failed_vote_persistence_does_not_grant_vote(tmp_path, monkeypatch):
    store = _store(tmp_path / "election.bin")

    def fail_save(kind, value):
        raise SecurityStoreError("Simulated storage failure.")

    monkeypatch.setattr(store._file, "save", fail_save)

    with pytest.raises(SecurityStoreError):
        store.grant_vote(1, "node-a")


def test_save_flushes_and_fsyncs_to_disk(tmp_path, monkeypatch):
    import os

    fsync_called = []
    real_fsync = os.fsync

    def tracking_fsync(fd):
        fsync_called.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    store = _store(tmp_path / "election.bin")
    assert store.grant_vote(1, "node-a") is True
    assert len(fsync_called) >= 1
