import pytest

from astral_remote.agent.network.election import (
    ELECTION_CANDIDATE,
    ELECTION_ELECTED,
    ELECTION_IDLE,
    ElectionCoordinator,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.security import (
    ElectionStateStore,
    XorTestProtector,
)


def _setup(tmp_path, member_ids=("node-a", "node-b", "node-c")):
    members = tuple(
        ElectionMember(
            installation_id=f"install-{node_id}",
            node_id=node_id,
        )
        for node_id in member_ids
    )

    store = ElectionStateStore(
        tmp_path / "election.bin",
        XorTestProtector(b"election-test-key"),
    )

    coordinator = ElectionCoordinator(
        member_ids[0],
        members,
        store,
    )
    return coordinator, store


def test_election_begins_with_persisted_self_vote(tmp_path):
    coordinator, store = _setup(tmp_path)

    state = coordinator.begin_election()

    assert state.term == 1
    assert state.phase == ELECTION_CANDIDATE
    assert state.candidate_node_id == "node-a"
    assert state.votes_received == frozenset({"node-a"})
    assert state.quorum == 2

    persisted = store.load()
    assert persisted.current_term == 1
    assert persisted.voted_for == "node-a"


def test_majority_of_configured_members_elects_candidate(tmp_path):
    coordinator, _ = _setup(tmp_path)
    state = coordinator.begin_election()

    assert coordinator.record_vote("node-b", state.term, True) is True

    result = coordinator.snapshot()
    assert result.phase == ELECTION_ELECTED
    assert result.votes_received == frozenset({"node-a", "node-b"})


def test_duplicate_and_unknown_votes_are_not_counted(tmp_path):
    coordinator, _ = _setup(tmp_path)
    state = coordinator.begin_election()

    assert coordinator.record_vote("unknown-node", state.term, True) is False
    assert coordinator.record_vote("node-b", state.term, False) is False
    assert coordinator.record_vote("node-b", state.term, True) is True
    assert coordinator.record_vote("node-b", state.term, True) is False

    assert len(coordinator.snapshot().votes_received) == 2


def test_higher_term_ends_campaign_and_is_persisted(tmp_path):
    coordinator, store = _setup(tmp_path)
    coordinator.begin_election()

    assert coordinator.record_vote("node-b", 2, True) is False

    state = coordinator.snapshot()
    assert state.term == 2
    assert state.phase == ELECTION_IDLE
    assert state.candidate_node_id is None
    assert state.votes_received == frozenset()

    persisted = store.load()
    assert persisted.current_term == 2
    assert persisted.voted_for is None


def test_single_member_election_wins_with_self_vote(tmp_path):
    coordinator, _ = _setup(tmp_path, ("node-a",))

    state = coordinator.begin_election()

    assert state.quorum == 1
    assert state.phase == ELECTION_ELECTED
    assert state.votes_received == frozenset({"node-a"})


def test_non_member_cannot_be_local_candidate(tmp_path):
    _, store = _setup(tmp_path)
    members = (
        ElectionMember("install-a", "node-a"),
        ElectionMember("install-b", "node-b"),
    )

    with pytest.raises(ValueError, match="Local node is not an election member"):
        ElectionCoordinator("node-unknown", members, store)


def test_abort_campaign_resets_candidate_to_idle(tmp_path):
    coordinator, store = _setup(tmp_path)
    state = coordinator.begin_election()
    assert state.phase == ELECTION_CANDIDATE
    assert state.term == 1

    assert coordinator.abort_campaign(1) is True
    snap = coordinator.snapshot()
    assert snap.phase == ELECTION_IDLE
    assert snap.candidate_node_id is None
    assert snap.votes_received == frozenset()
    assert snap.term == 1

    # Persisted store term and vote records are not erased or rolled back
    persisted = store.load()
    assert persisted.current_term == 1
    assert persisted.voted_for == "node-a"


def test_abort_campaign_does_not_reset_elected_campaign(tmp_path):
    coordinator, _ = _setup(tmp_path, ("node-a",))
    state = coordinator.begin_election()
    assert state.phase == ELECTION_ELECTED

    assert coordinator.abort_campaign(1) is False
    assert coordinator.snapshot().phase == ELECTION_ELECTED


def test_abort_campaign_protects_against_stale_term(tmp_path):
    coordinator, _ = _setup(tmp_path)
    coordinator.begin_election()  # term 1

    # Stale or incorrect term attempts do nothing
    assert coordinator.abort_campaign(0) is False
    assert coordinator.abort_campaign(2) is False
    assert coordinator.snapshot().phase == ELECTION_CANDIDATE


def test_subsequent_campaign_advances_term_and_records_new_self_vote(tmp_path):
    coordinator, store = _setup(tmp_path)
    state1 = coordinator.begin_election()
    assert state1.term == 1
    assert store.load().voted_for == "node-a"

    assert coordinator.abort_campaign(1) is True
    assert coordinator.snapshot().phase == ELECTION_IDLE

    # Second campaign advances term and records self-vote
    state2 = coordinator.begin_election()
    assert state2.term == 2
    assert state2.phase == ELECTION_CANDIDATE
    assert store.load().current_term == 2
    assert store.load().voted_for == "node-a"
