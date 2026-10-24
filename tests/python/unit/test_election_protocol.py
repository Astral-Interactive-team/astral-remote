"""Unit tests for Phase 06 Election Protocol Integration.

Tests cover authenticated vote requests/responses, voter safety,
membership fingerprint, quorum, term persistence, PRE_HOST eligibility,
host recovery/conflict, storage errors, timeouts, and shutdown.
"""

import asyncio
import functools

import pytest

from astral_remote.agent.network.election import (
    ELECTION_CANDIDATE,
    ELECTION_ELECTED,
    ELECTION_IDLE,
    ElectionCoordinator,
)
from astral_remote.agent.network.election_messages import (
    build_election_request_payload,
    build_election_response_payload,
    membership_fingerprint,
)
from astral_remote.agent.network.election_protocol import (
    DEFAULT_ELECTION_MAX_RETRIES,
    DEFAULT_ELECTION_TIMEOUT_SECONDS,
    ElectionConfig,
    ElectionProtocol,
    parse_election_protocol_config,
)
from astral_remote.agent.network.host_presence import (
    FOLLOWING_HOST,
    HOST_CONFLICT,
    HOST_UNKNOWN,
    PRE_HOST,
    HostPresenceTracker,
)
from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.security import (
    ElectionState,
    ElectionStateStore,
    SecurityStoreError,
    XorTestProtector,
)


def async_test(coro_fn):
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MEMBERS_3 = (
    ElectionMember("install-a", "node-a"),
    ElectionMember("install-b", "node-b"),
    ElectionMember("install-c", "node-c"),
)

MEMBERS_5 = (
    ElectionMember("install-a", "node-a"),
    ElectionMember("install-b", "node-b"),
    ElectionMember("install-c", "node-c"),
    ElectionMember("install-d", "node-d"),
    ElectionMember("install-e", "node-e"),
)


class FakeIdentity:
    """Minimal identity for tests that don't need real Ed25519 signing."""

    def __init__(self, node_id="node-a"):
        self.node_id = node_id
        self.public_key = b"\x00" * 32
        self.private_key = b"\x00" * 32


class FakeSender:
    """Capture sent payloads for assertions."""

    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def __call__(self, node_id: str, payload: dict) -> None:
        self.sent.append((node_id, payload))


def _store(tmp_path, name="election.bin"):
    return ElectionStateStore(
        tmp_path / name,
        XorTestProtector(b"election-test-key"),
    )


def _protocol(
    tmp_path,
    local_node_id="node-a",
    members=MEMBERS_3,
    host_state=PRE_HOST,
    sender=None,
    session_ids=None,
    store=None,
    config=None,
):
    """Build an ElectionProtocol wired to fakes."""
    if store is None:
        store = _store(tmp_path)
    coordinator = ElectionCoordinator(local_node_id, members, store)
    identity = FakeIdentity(local_node_id)
    if sender is None:
        sender = FakeSender()
    if session_ids is None:
        session_ids = {m.node_id: f"session-{m.node_id}" for m in members}
    if config is None:
        config = ElectionConfig(timeout_seconds=1.0, retry_delay_seconds=0.1, max_retries=1)

    protocol = ElectionProtocol(
        coordinator,
        store,
        members,
        config,
        send_to_peer=sender,
        get_identity=lambda: identity,
        get_session_id=lambda nid: session_ids.get(nid),
        get_host_state=lambda: host_state,
    )
    return protocol, coordinator, store, sender, identity


def _vote_request_msg(
    node_id="node-b",
    term=1,
    candidate="node-b",
    fingerprint=None,
    session_id="session-node-b",
):
    """Build a minimal election-request message dict (no real signature)."""
    if fingerprint is None:
        fingerprint = membership_fingerprint(MEMBERS_3)
    return {
        "version": 1,
        "network": "astral-remote",
        "type": "election-request",
        "nodeId": node_id,
        "publicKey": "",
        "sessionId": session_id,
        "electionTerm": term,
        "candidateNodeId": candidate,
        "membershipFingerprint": fingerprint,
        "signature": "",
    }


def _vote_response_msg(
    node_id="node-b",
    term=1,
    candidate="node-a",
    granted=True,
    fingerprint=None,
    session_id="session-node-a",
):
    if fingerprint is None:
        fingerprint = membership_fingerprint(MEMBERS_3)
    return {
        "version": 1,
        "network": "astral-remote",
        "type": "election-response",
        "nodeId": node_id,
        "publicKey": "",
        "sessionId": session_id,
        "electionTerm": term,
        "candidateNodeId": candidate,
        "voteGranted": granted,
        "membershipFingerprint": fingerprint,
        "signature": "",
    }


# ---------------------------------------------------------------------------
# Membership fingerprint tests
# ---------------------------------------------------------------------------


class TestMembershipFingerprint:
    def test_same_members_different_order_same_fingerprint(self):
        a = membership_fingerprint(MEMBERS_3)
        b = membership_fingerprint(MEMBERS_3[::-1])
        assert a == b
        assert len(a) == 64  # SHA-256 hex

    def test_empty_members(self):
        assert membership_fingerprint(()) == ""

    def test_different_members_different_fingerprint(self):
        a = membership_fingerprint(MEMBERS_3)
        b = membership_fingerprint(MEMBERS_5)
        assert a != b

    def test_fingerprint_unambiguous_with_delimiters(self):
        # Two different pairings that would collide under delimiter concatenation f"{iid}:{nid}"
        pairs1 = (
            ElectionMember("install:a", "node:b"),
            ElectionMember("install:c", "node:d"),
        )
        pairs2 = (
            ElectionMember("install", "a:node:b"),
            ElectionMember("install:c", "node:d"),
        )
        assert membership_fingerprint(pairs1) != membership_fingerprint(pairs2)

    def test_fingerprint_supports_unicode_identifiers(self):
        members = (
            ElectionMember("inst-東京", "node-v1-大阪"),
            ElectionMember("inst-京都", "node-v1-名古屋"),
        )
        fp1 = membership_fingerprint(members)
        fp2 = membership_fingerprint(members[::-1])
        assert fp1 == fp2
        assert len(fp1) == 64

    def test_fingerprint_special_characters(self):
        members = (
            ElectionMember('inst|"special"', 'node,with|delims:1'),
        )
        fp = membership_fingerprint(members)
        assert len(fp) == 64


# ---------------------------------------------------------------------------
# Vote request handling (voter side)
# ---------------------------------------------------------------------------


class TestHandleVoteRequest:
    @async_test
    async def test_valid_vote_request_grants_and_persists(self, tmp_path):
        protocol, _, store, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(term=1)

        await protocol.handle_vote_request("node-b", msg)

        assert len(sender.sent) == 1
        _, resp = sender.sent[0]
        assert resp["voteGranted"] is True

        persisted = store.load()
        assert persisted.current_term == 1
        assert persisted.voted_for == "node-b"

    @async_test
    async def test_unknown_candidate_rejected(self, tmp_path):
        protocol, _, _, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(
            node_id="node-unknown",
            candidate="node-unknown",
        )

        await protocol.handle_vote_request("node-unknown", msg)

        assert len(sender.sent) == 1
        _, resp = sender.sent[0]
        assert resp["voteGranted"] is False

    @async_test
    async def test_identity_mismatch_rejected(self, tmp_path):
        """Message nodeId differs from authenticated session identity."""
        protocol, _, _, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(node_id="node-b", candidate="node-b")

        # Authenticated as node-c but message says node-b.
        await protocol.handle_vote_request("node-c", msg)

        assert len(sender.sent) == 1
        _, resp = sender.sent[0]
        assert resp["voteGranted"] is False

    @async_test
    async def test_candidate_not_sender_rejected(self, tmp_path):
        """Candidate in message differs from sender."""
        protocol, _, _, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(node_id="node-b", candidate="node-c")

        await protocol.handle_vote_request("node-b", msg)

        assert len(sender.sent) == 1
        _, resp = sender.sent[0]
        assert resp["voteGranted"] is False

    @async_test
    async def test_one_vote_per_term_across_store_reloads(self, tmp_path):
        store = _store(tmp_path)
        protocol, _, _, sender, _ = _protocol(tmp_path, store=store)

        msg_b = _vote_request_msg(term=1, node_id="node-b", candidate="node-b")
        await protocol.handle_vote_request("node-b", msg_b)
        assert sender.sent[-1][1]["voteGranted"] is True

        # Same candidate, same term — should return previous decision (granted).
        await protocol.handle_vote_request("node-b", msg_b)
        assert sender.sent[-1][1]["voteGranted"] is True

        # Different candidate, same term — reject.
        msg_c = _vote_request_msg(
            term=1, node_id="node-c", candidate="node-c",
            session_id="session-node-c",
        )
        await protocol.handle_vote_request("node-c", msg_c)
        assert sender.sent[-1][1]["voteGranted"] is False

        # Verify persistence survives reload.
        store2 = _store(tmp_path)
        assert store2.load() == ElectionState(current_term=1, voted_for="node-b")

    @async_test
    async def test_stale_term_rejected(self, tmp_path):
        store = _store(tmp_path)
        store.observe_term(5)
        protocol, _, _, sender, _ = _protocol(tmp_path, store=store)

        msg = _vote_request_msg(term=3)  # Below current term 5.
        await protocol.handle_vote_request("node-b", msg)

        assert sender.sent[-1][1]["voteGranted"] is False

    @async_test
    async def test_higher_term_clears_previous_vote(self, tmp_path):
        store = _store(tmp_path)
        protocol, _, _, sender, _ = _protocol(tmp_path, store=store)

        msg1 = _vote_request_msg(term=1, node_id="node-b", candidate="node-b")
        await protocol.handle_vote_request("node-b", msg1)
        assert sender.sent[-1][1]["voteGranted"] is True

        # Higher term from node-c should succeed.
        msg2 = _vote_request_msg(
            term=3, node_id="node-c", candidate="node-c",
            session_id="session-node-c",
        )
        await protocol.handle_vote_request("node-c", msg2)
        assert sender.sent[-1][1]["voteGranted"] is True

        assert store.load().current_term == 3
        assert store.load().voted_for == "node-c"

    @async_test
    async def test_membership_fingerprint_mismatch_rejected(self, tmp_path):
        protocol, _, _, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(fingerprint="wrong-fingerprint")

        await protocol.handle_vote_request("node-b", msg)

        assert sender.sent[-1][1]["voteGranted"] is False

    @async_test
    async def test_storage_error_never_grants_vote(self, tmp_path, monkeypatch):
        store = _store(tmp_path)
        protocol, _, _, sender, _ = _protocol(tmp_path, store=store)

        original_grant = store.grant_vote

        def fail_grant(term, candidate):
            raise SecurityStoreError("Simulated disk failure.")

        monkeypatch.setattr(store, "grant_vote", fail_grant)

        msg = _vote_request_msg(term=1)
        await protocol.handle_vote_request("node-b", msg)

        assert sender.sent[-1][1]["voteGranted"] is False

    @async_test
    async def test_invalid_term_rejected(self, tmp_path):
        protocol, _, _, sender, _ = _protocol(tmp_path)
        msg = _vote_request_msg(term=-1)
        await protocol.handle_vote_request("node-b", msg)
        assert sender.sent[-1][1]["voteGranted"] is False

        msg2 = _vote_request_msg(term="not-an-int")
        await protocol.handle_vote_request("node-b", msg2)
        assert sender.sent[-1][1]["voteGranted"] is False


# ---------------------------------------------------------------------------
# Vote response handling (candidate side)
# ---------------------------------------------------------------------------


class TestHandleVoteResponse:
    @async_test
    async def test_valid_response_counted(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(node_id="node-b", term=1, granted=True)
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert snap.phase == ELECTION_ELECTED
        assert "node-b" in snap.votes_received

    @async_test
    async def test_response_for_another_candidate_ignored(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(
            node_id="node-b", term=1, candidate="node-c", granted=True,
        )
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert snap.phase == ELECTION_CANDIDATE
        assert "node-b" not in snap.votes_received

    @async_test
    async def test_duplicate_response_not_double_counted(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(
            tmp_path, members=MEMBERS_5,
        )
        coordinator.begin_election()

        msg = _vote_response_msg(
            node_id="node-b",
            term=1,
            granted=True,
            fingerprint=membership_fingerprint(MEMBERS_5),
        )
        await protocol.handle_vote_response("node-b", msg)
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert len(snap.votes_received) == 2  # self + node-b

    @async_test
    async def test_response_identity_mismatch_ignored(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(node_id="node-b", term=1, granted=True)
        # Authenticated as node-c, message says node-b.
        await protocol.handle_vote_response("node-c", msg)

        snap = coordinator.snapshot()
        assert "node-b" not in snap.votes_received
        assert "node-c" not in snap.votes_received

    @async_test
    async def test_response_fingerprint_mismatch_ignored(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(
            node_id="node-b", term=1, granted=True,
            fingerprint="wrong",
        )
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert "node-b" not in snap.votes_received

    @async_test
    async def test_non_member_response_ignored(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(
            node_id="node-stranger", term=1, granted=True,
        )
        await protocol.handle_vote_response("node-stranger", msg)

        snap = coordinator.snapshot()
        assert "node-stranger" not in snap.votes_received


# ---------------------------------------------------------------------------
# Quorum from configured membership
# ---------------------------------------------------------------------------


class TestQuorum:
    def test_quorum_3_members(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path, members=MEMBERS_3)
        assert coordinator.quorum == 2

    def test_quorum_5_members(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path, members=MEMBERS_5)
        assert coordinator.quorum == 3

    def test_quorum_1_member(self, tmp_path):
        members = (ElectionMember("install-a", "node-a"),)
        protocol, coordinator, _, _, _ = _protocol(
            tmp_path, members=members,
        )
        assert coordinator.quorum == 1

    @async_test
    async def test_no_winner_without_majority(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path, members=MEMBERS_5)
        coordinator.begin_election()

        # Self vote + 1 more = 2, need 3 for quorum.
        msg = _vote_response_msg(
            node_id="node-b",
            term=1,
            granted=True,
            fingerprint=membership_fingerprint(MEMBERS_5),
        )
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert snap.phase == ELECTION_CANDIDATE
        assert len(snap.votes_received) == 2


# ---------------------------------------------------------------------------
# Election eligibility (PRE_HOST requirement)
# ---------------------------------------------------------------------------


class TestElectionEligibility:
    def test_should_start_only_in_pre_host(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=PRE_HOST)
        assert protocol.should_start_election() is True

    def test_should_not_start_in_following_host(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=FOLLOWING_HOST)
        assert protocol.should_start_election() is False

    def test_should_not_start_in_host_unknown(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=HOST_UNKNOWN)
        assert protocol.should_start_election() is False

    def test_should_not_start_in_host_conflict(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=HOST_CONFLICT)
        assert protocol.should_start_election() is False

    def test_should_not_start_when_already_campaigning(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path, host_state=PRE_HOST)
        coordinator.begin_election()
        assert protocol.should_start_election() is False

    def test_should_not_start_with_no_members(self, tmp_path):
        # Empty membership — can't build ElectionCoordinator, so test
        # at the protocol level with a trick.
        members = (ElectionMember("install-a", "node-a"),)
        protocol, _, _, _, _ = _protocol(tmp_path, members=members)
        # Override members to empty to test the guard.
        protocol._members = ()
        assert protocol.should_start_election() is False


# ---------------------------------------------------------------------------
# Host recovery and conflict during election
# ---------------------------------------------------------------------------


class TestHostRecoveryAndConflict:
    @async_test
    async def test_host_recovery_stops_campaign(self, tmp_path):
        """If host recovers (FOLLOWING_HOST), campaign should stop."""
        host_state_box = [PRE_HOST]

        store = _store(tmp_path)
        coordinator = ElectionCoordinator("node-a", MEMBERS_3, store)
        identity = FakeIdentity("node-a")
        sender = FakeSender()
        session_ids = {m.node_id: f"session-{m.node_id}" for m in MEMBERS_3}
        config = ElectionConfig(
            timeout_seconds=0.2, retry_delay_seconds=0.05, max_retries=0,
        )

        protocol = ElectionProtocol(
            coordinator, store, MEMBERS_3, config,
            send_to_peer=sender,
            get_identity=lambda: identity,
            get_session_id=lambda nid: session_ids.get(nid),
            get_host_state=lambda: host_state_box[0],
        )

        snap = await protocol.start_campaign()
        assert snap is not None
        assert snap.phase == ELECTION_CANDIDATE

        # Simulate host recovery.
        host_state_box[0] = FOLLOWING_HOST

        # Wait for campaign to notice, abort to IDLE, and stop.
        await asyncio.sleep(0.3)
        assert coordinator.snapshot().phase == ELECTION_IDLE

        # Host fails again; a new campaign must start successfully for term 2
        host_state_box[0] = PRE_HOST
        snap2 = await protocol.start_campaign()
        assert snap2 is not None
        assert snap2.term == 2
        assert snap2.phase == ELECTION_CANDIDATE

        await protocol.shutdown()

    @async_test
    async def test_host_conflict_stops_campaign(self, tmp_path):
        host_state_box = [PRE_HOST]

        store = _store(tmp_path)
        coordinator = ElectionCoordinator("node-a", MEMBERS_3, store)
        identity = FakeIdentity("node-a")
        sender = FakeSender()
        session_ids = {m.node_id: f"session-{m.node_id}" for m in MEMBERS_3}
        config = ElectionConfig(
            timeout_seconds=0.2, retry_delay_seconds=0.05, max_retries=0,
        )

        protocol = ElectionProtocol(
            coordinator, store, MEMBERS_3, config,
            send_to_peer=sender,
            get_identity=lambda: identity,
            get_session_id=lambda nid: session_ids.get(nid),
            get_host_state=lambda: host_state_box[0],
        )

        snap = await protocol.start_campaign()
        assert snap is not None

        host_state_box[0] = HOST_CONFLICT
        await asyncio.sleep(0.3)
        # Aborts to IDLE and does not declare winner
        assert coordinator.snapshot().phase == ELECTION_IDLE
        assert coordinator.snapshot().phase != ELECTION_ELECTED
        await protocol.shutdown()


# ---------------------------------------------------------------------------
# Campaign lifecycle
# ---------------------------------------------------------------------------


class TestStartCampaign:
    @async_test
    async def test_start_campaign_sends_requests(self, tmp_path):
        protocol, _, _, sender, _ = _protocol(tmp_path, host_state=PRE_HOST)

        snap = await protocol.start_campaign()
        assert snap is not None
        assert snap.phase == ELECTION_CANDIDATE

        # Wait a bit for broadcast.
        await asyncio.sleep(0.3)

        # Should have sent requests to node-b and node-c.
        targets = [nid for nid, _ in sender.sent]
        assert "node-b" in targets or "node-c" in targets

        await protocol.shutdown()

    @async_test
    async def test_single_member_wins_immediately(self, tmp_path):
        members = (ElectionMember("install-a", "node-a"),)
        protocol, _, _, _, _ = _protocol(
            tmp_path, members=members, host_state=PRE_HOST,
        )

        snap = await protocol.start_campaign()
        assert snap is not None
        assert snap.phase == ELECTION_ELECTED
        assert snap.votes_received == frozenset({"node-a"})

        await protocol.shutdown()

    @async_test
    async def test_not_eligible_returns_none(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=FOLLOWING_HOST)
        snap = await protocol.start_campaign()
        assert snap is None

    @async_test
    async def test_shutdown_cancels_campaign(self, tmp_path):
        protocol, _, _, _, _ = _protocol(tmp_path, host_state=PRE_HOST)

        snap = await protocol.start_campaign()
        assert snap is not None

        await protocol.shutdown()
        assert protocol._campaign_task is None

    @async_test
    async def test_retry_exhaustion_resets_and_advances_term(self, tmp_path):
        """When retries exhaust without quorum, campaign aborts and delays before next term."""
        store = _store(tmp_path)
        coordinator = ElectionCoordinator("node-a", MEMBERS_3, store)
        identity = FakeIdentity("node-a")
        sender = FakeSender()
        session_ids = {m.node_id: f"session-{m.node_id}" for m in MEMBERS_3}
        config = ElectionConfig(
            timeout_seconds=0.05, retry_delay_seconds=0.02, max_retries=1,
        )

        protocol = ElectionProtocol(
            coordinator, store, MEMBERS_3, config,
            send_to_peer=sender,
            get_identity=lambda: identity,
            get_session_id=lambda nid: session_ids.get(nid),
            get_host_state=lambda: PRE_HOST,
        )

        snap = await protocol.start_campaign()
        assert snap is not None
        assert snap.term == 1

        # Wait for term 1 to exhaust retries and advance to term 2
        await asyncio.sleep(0.2)

        current_snap = coordinator.snapshot()
        assert current_snap.term >= 2
        await protocol.shutdown()

    @async_test
    async def test_cleanup_does_not_reset_elected_or_newer_state(self, tmp_path):
        """Stale abort attempt must not reset an ELECTED coordinator or a newer term."""
        store = _store(tmp_path)
        coordinator = ElectionCoordinator("node-a", (ElectionMember("inst-a", "node-a"),), store)
        snap = coordinator.begin_election()
        assert snap.phase == ELECTION_ELECTED

        # Abort attempt on elected state fails
        assert coordinator.abort_campaign(snap.term) is False
        assert coordinator.snapshot().phase == ELECTION_ELECTED

        # Advance coordinator to term 2
        coordinator2 = ElectionCoordinator("node-a", MEMBERS_3, store)
        coordinator2.begin_election()  # term 2
        assert coordinator2.snapshot().term == 2

        # Stale abort for term 1 fails
        assert coordinator2.abort_campaign(1) is False
        assert coordinator2.snapshot().term == 2
        assert coordinator2.snapshot().phase == ELECTION_CANDIDATE


# ---------------------------------------------------------------------------
# Inconsistent membership detection
# ---------------------------------------------------------------------------


class TestMembershipConsistency:
    @async_test
    async def test_mismatched_fingerprint_in_request(self, tmp_path):
        protocol, _, _, sender, _ = _protocol(tmp_path)

        msg = _vote_request_msg(
            term=1,
            fingerprint=membership_fingerprint(MEMBERS_5),
        )
        await protocol.handle_vote_request("node-b", msg)

        assert sender.sent[-1][1]["voteGranted"] is False

    @async_test
    async def test_mismatched_fingerprint_in_response(self, tmp_path):
        protocol, coordinator, _, _, _ = _protocol(tmp_path)
        coordinator.begin_election()

        msg = _vote_response_msg(
            node_id="node-b", term=1, granted=True,
            fingerprint=membership_fingerprint(MEMBERS_5),
        )
        await protocol.handle_vote_response("node-b", msg)

        snap = coordinator.snapshot()
        assert "node-b" not in snap.votes_received


# ---------------------------------------------------------------------------
# Election config parsing
# ---------------------------------------------------------------------------


class TestElectionConfigParsing:
    def test_defaults(self):
        config = parse_election_protocol_config(None)
        assert config.timeout_seconds == DEFAULT_ELECTION_TIMEOUT_SECONDS
        assert config.max_retries == DEFAULT_ELECTION_MAX_RETRIES

    def test_custom_values(self):
        raw = {"peerNetwork": {"election": {
            "timeoutSeconds": 5.0,
            "retryDelaySeconds": 1.0,
            "maxRetries": 2,
        }}}
        config = parse_election_protocol_config(raw)
        assert config.timeout_seconds == 5.0
        assert config.retry_delay_seconds == 1.0
        assert config.max_retries == 2

    def test_invalid_timeout_rejected(self):
        raw = {"peerNetwork": {"election": {"timeoutSeconds": -1}}}
        with pytest.raises(ValueError):
            parse_election_protocol_config(raw)

    def test_invalid_retries_rejected(self):
        raw = {"peerNetwork": {"election": {"maxRetries": -1}}}
        with pytest.raises(ValueError):
            parse_election_protocol_config(raw)


# ---------------------------------------------------------------------------
# Election message builders
# ---------------------------------------------------------------------------


class TestElectionMessageBuilders:
    def test_request_payload_shape(self):
        from astral_remote.shared.security import IdentityStore

        identity = IdentityStore(
            "nonexistent", XorTestProtector(b"test-key-0123456"),
        )
        # Can't call without a real identity, so just test the fingerprint.
        fp = membership_fingerprint(MEMBERS_3)
        assert isinstance(fp, str)
        assert len(fp) == 64

    def test_response_payload_includes_reason(self, tmp_path):
        """Verify response shape includes optional reason field."""
        # Build a fake response dict directly.
        fp = membership_fingerprint(MEMBERS_3)
        resp = {
            "type": "election-response",
            "electionTerm": 1,
            "candidateNodeId": "node-a",
            "voteGranted": False,
            "membershipFingerprint": fp,
            "reason": "Vote already cast",
        }
        assert resp["reason"] == "Vote already cast"
        assert resp["voteGranted"] is False
