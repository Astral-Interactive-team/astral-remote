"""Local election coordination without network transport."""

from dataclasses import dataclass

from astral_remote.agent.network.models import ElectionMember
from astral_remote.shared.security import (
    ElectionStateStore,
    SecurityStoreError,
)

ELECTION_IDLE = "IDLE"
ELECTION_CANDIDATE = "CANDIDATE"
ELECTION_ELECTED = "ELECTED"


@dataclass(frozen=True)
class ElectionSnapshot:
    """Immutable snapshot of the local election state."""

    term: int
    phase: str
    candidate_node_id: str | None
    votes_received: frozenset[str]
    quorum: int


class ElectionCoordinator:
    """Track a candidate's election term and distinct granted votes.

    The caller must supply voter identities obtained from authenticated
    peer sessions. This class does not send or authenticate network messages.
    """

    def __init__(
        self,
        local_node_id: str,
        members: tuple[ElectionMember, ...],
        state_store: ElectionStateStore,
    ) -> None:
        if (
            not isinstance(local_node_id, str)
            or not local_node_id.strip()
            or local_node_id != local_node_id.strip()
        ):
            raise ValueError("Local election node ID is invalid.")

        if not members:
            raise ValueError("Election membership must not be empty.")

        installation_ids = [member.installation_id for member in members]
        node_ids = [member.node_id for member in members]

        if (
            any(not isinstance(member, ElectionMember) for member in members)
            or any(not isinstance(value, str) or not value.strip()
                   for value in installation_ids + node_ids)
            or len(set(installation_ids)) != len(installation_ids)
            or len(set(node_ids)) != len(node_ids)
        ):
            raise ValueError("Election membership is invalid.")

        if local_node_id not in node_ids:
            raise ValueError("Local node is not an election member.")

        self._local_node_id = local_node_id
        self._member_node_ids = frozenset(node_ids)
        self._state_store = state_store
        self._quorum = len(members) // 2 + 1

        persisted = state_store.load()
        self._term = persisted.current_term
        self._phase = ELECTION_IDLE
        self._candidate_node_id: str | None = None
        self._votes_received: set[str] = set()

    @property
    def quorum(self) -> int:
        """Return the fixed majority threshold for configured membership."""
        return self._quorum

    def snapshot(self) -> ElectionSnapshot:
        """Return an immutable view of the current election."""
        return ElectionSnapshot(
            term=self._term,
            phase=self._phase,
            candidate_node_id=self._candidate_node_id,
            votes_received=frozenset(self._votes_received),
            quorum=self._quorum,
        )

    def begin_election(self) -> ElectionSnapshot:
        """Persist a new term and self-vote before starting the campaign."""
        persisted = self._state_store.load()
        term = persisted.current_term + 1

        self._state_store.observe_term(term)

        if not self._state_store.grant_vote(term, self._local_node_id):
            raise SecurityStoreError("Election self-vote could not be recorded.")

        self._term = term
        self._candidate_node_id = self._local_node_id
        self._votes_received = {self._local_node_id}
        self._phase = (
            ELECTION_ELECTED
            if len(self._votes_received) >= self._quorum
            else ELECTION_CANDIDATE
        )

        return self.snapshot()

    def record_vote(
        self,
        voter_node_id: str,
        term: int,
        granted: bool,
    ) -> bool:
        """Count one granted vote from an eligible member for this term.

        Returns True only when a new vote is counted.
        A higher term ends the current campaign and is persisted.
        """
        if type(term) is not int or term < 0:
            return False

        if term > self._term:
            persisted = self._state_store.observe_term(term)
            self._term = persisted.current_term
            self._phase = ELECTION_IDLE
            self._candidate_node_id = None
            self._votes_received.clear()
            return False

        if (
            self._phase != ELECTION_CANDIDATE
            or term != self._term
            or granted is not True
            or not isinstance(voter_node_id, str)
            or voter_node_id not in self._member_node_ids
            or voter_node_id in self._votes_received
        ):
            return False

        self._votes_received.add(voter_node_id)

        if len(self._votes_received) >= self._quorum:
            self._phase = ELECTION_ELECTED

        return True

    def abort_campaign(self, expected_term: int) -> bool:
        """Abort an unsuccessful campaign and return coordinator to IDLE.

        Resets in-memory candidate and collected votes only if the coordinator
        is currently in CANDIDATE phase and the term matches expected_term.
        Does not roll back or erase persistent term/vote records in the store.
        Does not reset a campaign that has already reached ELECTED or has
        moved to a newer term.
        """
        if (
            type(expected_term) is not int
            or expected_term < 1
            or self._phase != ELECTION_CANDIDATE
            or self._term != expected_term
        ):
            return False

        self._phase = ELECTION_IDLE
        self._candidate_node_id = None
        self._votes_received.clear()
        return True
