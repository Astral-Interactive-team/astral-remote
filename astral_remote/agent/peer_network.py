"""Direct TLS peer transport with Node Identity mutual authentication.

This is intentionally a small, configured mesh transport: no DHT, relay,
NAT traversal, HTTP, or WebSocket service is claimed here.  Authenticated
sessions also carry a lightweight heartbeat/ acknowledgement exchange so
presence and staleness are observed on the same framing as authentication.

Phase 05 adds a separate Host Presence layer on top of the same framing:
host-specific heartbeats observe whether the currently recognised Host is
responding, and drive a state machine that can transition this node to
PRE_HOST when the Host appears to have failed.  Host election is never
performed here.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import os
import ssl
import tempfile
import time
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from astral_remote.agent.network.authentication import (
    PeerAuthenticator,
    _sign,
    _verify
)

from astral_remote.agent.network.codec import (
    _canon,
    _receive,
    _send,
)

from astral_remote.agent.network.config import (
    parse_network_config,
    parse_heartbeat_config,
    parse_election_members
)

from astral_remote.agent.network.election import (
    ElectionCoordinator,
    ElectionSnapshot,
)

from astral_remote.agent.network.election_messages import (
    build_election_request_payload,
    build_election_response_payload,
    build_lease_renewal_request_payload,
    build_lease_renewal_response_payload,
    build_agent_command_payload,
    build_agent_command_reply_payload,
    membership_fingerprint,
)

from astral_remote.agent.network.host_role import (
    STANDBY,
    ACTIVE,
    ACTIVATING,
    STEPPING_DOWN,
    FENCED,
    HostAuthorityConfig,
    HostLifecycleManager,
    HostRoleStatus,
    parse_host_authority_config,
)

from astral_remote.agent.network.election_protocol import (
    ElectionConfig,
    ElectionProtocol,
    parse_election_protocol_config,
)

from astral_remote.agent.network.constants import (
    PROTOCOL_VERSION,
    CONNECT_TIMEOUT_SECONDS,
    DISCONNECT_STATUS_SECONDS,
    MAX_MESSAGE_BYTES,
    NETWORK_NAME,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_RESPONSE_TIMEOUT_SECONDS,
    _HEARTBEAT_STALE,
    _HEARTBEAT_ACTIVE,
    _HEARTBEAT_WAITING,
    _HEARTBEAT_FAILED,
    _HEARTBEAT_MAX_RETRIES,
    MAX_CLOCK_SKEW_SECONDS,
)

from astral_remote.agent.network.errors import PeerProtocolError

from astral_remote.agent.network.heartbeat import (
    build_host_heartbeat_ack_payload,
    build_host_heartbeat_payload,
    build_peer_heartbeat_ack_payload,
    build_peer_heartbeat_payload,
)

from astral_remote.agent.network.host_presence import (
    FOLLOWING_HOST,
    HOST_CONFLICT,
    HOST_SUSPECTED,
    HOST_UNKNOWN,
    PRE_HOST,
    HostPresenceConfig,
    HostPresenceStatus,
    HostPresenceTracker,
    parse_host_presence_config,
)

from astral_remote.agent.network.models import (
    BootstrapPeer,
    PeerHeartbeat,
    PeerStatus
)

from astral_remote.agent.network.session import (
    _utc_now_text,
    PeerSession
)

from astral_remote.agent.network.transport import PeerTransport

from astral_remote.shared.logging_utils import info, warn, error
from astral_remote.shared.security import ElectionStateStore, NodeIdentity, SecurityStoreError, TrustStore, node_id_for

class PeerNetwork:
    """Agent-owned direct TLS listener and configured bootstrap connections."""

    def __init__(
        self,
        identity: NodeIdentity,
        trust_store: TrustStore,
        config: dict[str, Any] | None = None,
        *,
        installation_id: str | None = None,
        election_state_store: ElectionStateStore | None = None,
        agent_runtime: Any = None,
        host_runtime: Any = None,
        credential_store: Any = None,
        clock: Any = None,
        wall_clock: Any = None,
    ):
        self.identity, self.trust_store = identity, trust_store
        self._authenticator = PeerAuthenticator(self.identity, self.trust_store)
        self.enabled, self.bind_host, self.bind_port, self.bootstraps = parse_network_config(config)
        self.heartbeat_interval, self.heartbeat_response_timeout = parse_heartbeat_config(config)
        self._host_presence_config = parse_host_presence_config(config)
        self._host_tracker = HostPresenceTracker(self._host_presence_config)
        self._host_session: PeerSession | None = None
        self._server: asyncio.AbstractServer | None = None
        self._peers: dict[str, PeerStatus] = {}
        self._sessions: dict[str, PeerSession] = {}
        self._tasks: set[asyncio.Task] = set()
        self._transport = PeerTransport()
        self._agent_runtime = agent_runtime
        self._host_runtime = host_runtime
        self._credential_store = credential_store
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._pending_command_futures: dict[str, asyncio.Future] = {}
        self.installation_id = (
            installation_id.strip()
            if isinstance(installation_id, str) and installation_id.strip()
            else None
        )
        self._election_members = parse_election_members(config)
        self._election_protocol: ElectionProtocol | None = None
        self._host_lifecycle: HostLifecycleManager | None = None
        self._host_authority_config = parse_host_authority_config(config)

        if self._election_members:
            if self.installation_id is None:
                raise PeerProtocolError(
                    "Election membership requires an installation ID."
                )
            local_member = next(
                (
                    member
                    for member in self._election_members
                    if member.installation_id == self.installation_id
                ),
                None,
            )
            if local_member is None or local_member.node_id != self.identity.node_id:
                raise PeerProtocolError(
                    "Local node does not match election membership."
                )
            election_config = parse_election_protocol_config(config)
            if election_state_store is not None:
                self._election_state_store = election_state_store
                coordinator = ElectionCoordinator(
                    self.identity.node_id,
                    self._election_members,
                    election_state_store,
                )
                self._host_lifecycle = HostLifecycleManager(
                    self.identity.node_id,
                    self.installation_id,
                    self._election_members,
                    election_state_store,
                    coordinator=coordinator,
                    config=self._host_authority_config,
                    host_runtime=self._host_runtime,
                    credential_store=self._credential_store,
                    clock=self._clock,
                    wall_clock=self._wall_clock,
                    broadcast_renewal=self._broadcast_lease_renewal,
                )
                self._election_protocol = ElectionProtocol(
                    coordinator,
                    election_state_store,
                    self._election_members,
                    election_config,
                    send_to_peer=self._send_to_peer,
                    get_identity=lambda: self.identity,
                    get_session_id=self._get_session_id_for,
                    get_host_state=lambda: self._host_tracker.state,
                    on_elected=self._handle_election_won,
                    clock=self._clock,
                    lease_duration=self._host_authority_config.lease_duration_seconds,
                    reboot_cooldown=self._host_authority_config.reboot_cooldown_seconds,
                    enforce_lease=True,
                )
        else:
            self._election_state_store = election_state_store

    @property
    def listening_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    def status(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the network, peers, and host.

        The dictionary shape is intentionally consumer-friendly and
        backwards-compatible with the Phase 03/04 contract.  Peers appear
        in insertion order; each entry carries the TCP/TLS connection
        state (`connected`) and a separate heartbeat presence state
        (`heartbeat.state`).  `hostPresence` is a Phase 05 block describing
        the currently recognised Host and monitoring policy.
        """
        peers = []
        for node_id, status in self._peers.items():
            data = status.__dict__.copy()
            if data.get("heartbeat") is not None:
                data["heartbeat"] = data["heartbeat"].__dict__.copy()
            peers.append(data)
        host_status = self._host_tracker.status()
        result = {
            "enabled": self.enabled,
            "listening": self._server is not None,
            "bindHost": self.bind_host,
            "port": self.listening_port,
            "protocolVersion": PROTOCOL_VERSION,
            "heartbeatInterval": self.heartbeat_interval,
            "heartbeatResponseTimeout": self.heartbeat_response_timeout,
            "hostPresence": {
                "state": host_status.state,
                "hostNodeId": host_status.host_node_id,
                "consecutiveFailures": host_status.consecutive_failures,
                "consecutiveSuccesses": host_status.consecutive_successes,
                "lastAckAt": host_status.last_ack_at,
                "lastAckAgeSeconds": host_status.last_ack_age_seconds,
                "transitionReason": host_status.transition_reason,
                "lastError": host_status.last_error,
                "heartbeatInterval": self._host_presence_config.heartbeat_interval_seconds,
                "responseTimeout": self._host_presence_config.response_timeout_seconds,
                "failureThreshold": self._host_presence_config.failure_threshold,
                "recoveryThreshold": self._host_presence_config.recovery_threshold,
            },
            "peers": peers,
        }
        if self._election_protocol is not None:
            snap = self._election_protocol.snapshot()
            result["election"] = {
                "term": snap.term,
                "phase": snap.phase,
                "candidateNodeId": snap.candidate_node_id,
                "votesReceived": len(snap.votes_received),
                "quorum": snap.quorum,
                "membershipFingerprint": self._election_protocol.fingerprint,
            }
        if self._host_lifecycle is not None:
            snap_role = self._host_lifecycle.status()
            result["hostRole"] = {
                "role": snap_role.role,
                "term": snap_role.term,
                "isAuthoritative": snap_role.is_authoritative,
                "leaseExpiresAt": snap_role.lease_expires_at,
                "leaseRemainingSeconds": snap_role.lease_remaining_seconds,
                "consecutiveRenewalFailures": snap_role.consecutive_renewal_failures,
                "condition": snap_role.condition,
                "transitionReason": snap_role.transition_reason,
            }
        return result

    def host_status(self) -> dict[str, Any]:
        """Return only the Phase 05 host presence diagnostic block."""
        return self.status()["hostPresence"]

    @property
    def host_lifecycle(self) -> HostLifecycleManager | None:
        return self._host_lifecycle

    @property
    def host_role(self) -> str:
        return self._host_lifecycle.role if self._host_lifecycle is not None else STANDBY

    def is_host_authoritative(self) -> bool:
        return self._host_lifecycle.is_authoritative() if self._host_lifecycle is not None else False

    def set_host_runtime(self, runtime: Any) -> None:
        self._host_runtime = runtime
        if self._host_lifecycle is not None:
            self._host_lifecycle.set_host_runtime(runtime)

    async def start(self) -> None:
        if not self.enabled or self._server is not None:
            return
        self._server = await self._transport.start_server(
            self._accept,
            self.bind_host,
            self.bind_port,
        )
        for peer in self.bootstraps:
            info(
                "Peer bootstrap connect scheduled.",
                {
                    "nodeId": peer.node_id,
                    "host": peer.host,
                    "port": peer.port,
                },
            )
            self._spawn(self.connect(peer))

    async def close(self) -> None:
        if self._host_lifecycle is not None:
            await self._host_lifecycle.demote(reason="Network closing")
        if self._election_protocol is not None:
            await self._election_protocol.shutdown()
        self._host_tracker.reset()
        self._host_session = None
        for session in tuple(self._sessions.values()):
            await self._shutdown_session(session)
            session.close()
        self._sessions.clear()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._transport.close()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _ssl_context(self) -> ssl.SSLContext:
        # Python ssl needs file paths; this unrelated ephemeral transport key
        # exists only for live TLS process lifetime. Node key stays DPAPI-held.
        cert_key = Ed25519PrivateKey.generate()
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Astral Remote ephemeral transport")])
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(cert_key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=1))
                .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
                .sign(cert_key, algorithm=None))
        cert_file = tempfile.NamedTemporaryFile(prefix="astral-peer-", suffix=".pem", delete=False)
        key_file = tempfile.NamedTemporaryFile(prefix="astral-peer-", suffix=".key", delete=False)
        cert_file.write(cert.public_bytes(serialization.Encoding.PEM))
        cert_file.close()
        key_file.write(cert_key.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        key_file.close()
        self._cert_files = (cert_file.name, key_file.name)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.load_cert_chain(cert_file.name, key_file.name)
        return context

    def _client_ssl_context(self) -> ssl.SSLContext:
        if self._cert_files is None:
            self._ssl_context()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.load_cert_chain(*self._cert_files)
        return context

    async def connect(self, peer: BootstrapPeer) -> bool:
        """Open and authenticate one bootstrap peer session.

        Returns True only when the session was created.  Existing sessions
        for the same node are never duplicated.
        """
        if not self.enabled or self._sessions.get(peer.node_id) is not None:
            return False
        if not self.trust_store.is_trusted(peer.node_id, peer.public_key):
            self._peers[peer.node_id] = PeerStatus(peer.node_id, False, "direct-tls",
                                                   "Bootstrap peer is not trusted.")
            return False
        session_id = os.urandom(16).hex()
        reader = writer = None
        session: PeerSession | None = None
        try:
            reader, writer = await self._transport.open_connection(
                peer.host,
                peer.port,
            )

            remote_node_id, session_id = await self._authenticator.authenticate(
                reader,
                writer,
                initiator=True,
                expected=peer,
                session_id=session_id,
            )
            if remote_node_id != peer.node_id:
                raise PeerProtocolError("Bootstrap peer identity does not match.")
            session = PeerSession(peer.node_id, session_id, reader, writer,
                                  self.heartbeat_interval, self.heartbeat_response_timeout)
            self._sessions[peer.node_id] = session
            self._refresh_status(session)
            info("Peer session established.", {"nodeId": peer.node_id, "initiator": True})
            self._spawn(self._run_peer_session(session))
            return True
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                PeerProtocolError, SecurityStoreError) as exc:
            self._peers[peer.node_id] = PeerStatus(peer.node_id, False, "direct-tls", str(exc))
            warn("Peer connection failed.", {"nodeId": peer.node_id, "error": str(exc)})
            return False
        finally:
            # When the session was registered, the session task owns the
            # socket; otherwise close the raw transport here.
            if writer is not None and session is None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except (OSError, RuntimeError):
                    pass

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle one inbound TLS session from an already-trusted peer."""
        session: PeerSession | None = None
        try:
            node_id, session_id = node_id, session_id = await self._authenticator.authenticate(
                reader,
                writer,
                initiator=False,
            )
            if self._sessions.get(node_id) is not None:
                raise PeerProtocolError("Peer session already exists.")
            session = PeerSession(node_id, session_id, reader, writer,
                                  self.heartbeat_interval, self.heartbeat_response_timeout)
            self._sessions[node_id] = session
            self._refresh_status(session)
            info("Peer session established.", {"nodeId": node_id, "initiator": False})
            # The session task owns the socket from this point on.
            self._spawn(self._run_peer_session(session))
        except (PeerProtocolError, SecurityStoreError, asyncio.IncompleteReadError, OSError) as exc:
            session = None
            warn("Peer authentication failed.", {"error": str(exc)})
        finally:
            if session is None and writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except (OSError, RuntimeError):
                    pass

    def _status_for(self, session: PeerSession | None) -> PeerStatus:
        """Build a PeerStatus from a session, separating socket and presence."""
        if session is None:
            return PeerStatus("", False, "direct-tls")
        monotonic_now = time.monotonic()
        last_sent_age = (monotonic_now - session.last_heartbeat_time
                         if session.last_heartbeat_time is not None else None)
        last_ack_age = (monotonic_now - session.last_ack_time
                        if session.last_ack_time is not None else None)
        last_received_age = (monotonic_now - session.last_received_time
                             if session.last_received_time is not None else None)

        if not session.active:
            state = _HEARTBEAT_FAILED if session.last_error else _HEARTBEAT_STALE
        elif session.last_error is not None and session.last_ack_time is None:
            state = _HEARTBEAT_FAILED
        elif last_ack_age is None:
            # No ACK has ever arrived.  Waiting is the honest state until the
            # response timeout elapses, after which the peer is stale.
            if (session.pending_since is not None
                    and monotonic_now - session.pending_since > DISCONNECT_STATUS_SECONDS):
                state = _HEARTBEAT_STALE
            else:
                state = _HEARTBEAT_WAITING
        elif last_ack_age > DISCONNECT_STATUS_SECONDS:
            state = _HEARTBEAT_STALE
        else:
            state = _HEARTBEAT_ACTIVE

        return PeerStatus(
            node_id=session.node_id,
            connected=session.active,
            transport="direct-tls",
            error=session.last_error,
            heartbeat=PeerHeartbeat(
                state=state,
                interval_seconds=session.interval,
                response_timeout_seconds=session.response_timeout,
                sequence=session.pending_sequence,
                last_sent_at=session.last_heartbeat_wall,
                last_received_at=session.last_received_wall,
                last_ack_at=session.last_ack_wall,
                last_ack_sequence=session.last_ack_sequence,
                last_heartbeat_age_seconds=last_sent_age,
                last_received_age_seconds=last_received_age,
                last_ack_age_seconds=last_ack_age,
                last_error=session.last_error,
            ),
        )

    def _refresh_status(self, session: PeerSession) -> None:
        self._peers[session.node_id] = self._status_for(session)

    async def _run_peer_session(self, session: PeerSession) -> None:
        """Owning task for one authenticated session.

        Runs the reader loop and the heartbeat sender concurrently.  The
        host heartbeat monitor is a separate task that runs only while a
        host session is being tracked.  A peer-specific failure never
        cancels the network or other peer sessions.
        """
        reader_task = asyncio.create_task(self._run_reader(session))
        heartbeat_task = asyncio.create_task(self._run_heartbeat(session))
        host_monitor_task = asyncio.create_task(self._run_host_monitor(session))
        try:
            done, pending = await asyncio.wait(
                (reader_task, heartbeat_task, host_monitor_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if exc is not None and not isinstance(exc, (asyncio.CancelledError,)):
                    session.record_error(str(exc))
        except asyncio.CancelledError:
            for task in (reader_task, heartbeat_task, host_monitor_task):
                task.cancel()
            await asyncio.gather(reader_task, heartbeat_task, host_monitor_task,
                                 return_exceptions=True)
            raise
        finally:
            if self._host_session is session:
                self._host_session = None
            await self._shutdown_session(session)
            self._refresh_status(session)

    async def _run_reader(self, session: PeerSession) -> None:
        """Read and validate session messages until the session closes."""
        try:
            while session.active:
                message = await _receive(session.reader)
                await self._handle_session_message(session, message)
        except asyncio.IncompleteReadError:
            if session.active:
                session.record_error("Peer connection closed.")
        except (OSError, PeerProtocolError, asyncio.TimeoutError) as exc:
            if session.active:
                session.record_error(str(exc))

    async def _run_heartbeat(self, session: PeerSession) -> None:
        """Send peer heartbeats, re-send on timeout, and tear down after failures."""
        retries = 0
        try:
            while session.active:
                if session.pending_sequence is None:
                    try:
                        await self._send_heartbeat(session)
                    except (OSError, asyncio.TimeoutError, PeerProtocolError) as exc:
                        if session.active:
                            session.record_error("Heartbeat send failed: " + str(exc))
                        break
                    await asyncio.sleep(session.interval)
                    continue
                await asyncio.sleep(session.response_timeout)
                if not session.active:
                    break
                if session.pending_sequence is None:
                    retries = 0
                    continue
                retries += 1
                if retries >= _HEARTBEAT_MAX_RETRIES:
                    session.record_error("Heartbeat ACK not received after retries.")
                    warn("Heartbeat timeout; closing peer session.",
                         {"nodeId": session.node_id})
                    break
                try:
                    await self._send_locked(
                        session,
                        build_peer_heartbeat_payload(
                            self.identity,
                            session.session_id,
                            session.pending_sequence,
                        ),
                    )
                    info("Heartbeat re-sent.", {"nodeId": session.node_id,
                                                "sequence": session.pending_sequence,
                                                "retry": retries})
                except (OSError, asyncio.TimeoutError, PeerProtocolError) as exc:
                    if session.active:
                        session.record_error("Heartbeat re-send failed: " + str(exc))
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.record_error("Heartbeat loop error: " + str(exc))
            error("Heartbeat loop stopped.",
                  {"nodeId": session.node_id, "sessionId": session.session_id,
                   "error": str(exc)})

    async def _run_host_monitor(self, session: PeerSession) -> None:
        """Send host heartbeats to the recognised Host and drive the tracker.

        This task is spawned for every authenticated session but only acts
        when the session's node is the currently recognised Host.  The
        host tracker's consecutive-failure policy drives transitions to
        PRE_HOST.  The monitor never elects a Host and never selects one
        when the tracker is HOST_UNKNOWN.
        """
        try:
            while session.active:
                # If this session is not the recognised Host, sleep and
                # re-check.  A newly configured Host may be this node.
                if self._host_session is not session:
                    if (self._host_tracker.host_node_id is not None
                            and self._host_tracker.host_node_id == session.node_id):
                        self._host_session = session
                        info("Host session now monitored.", {"nodeId": session.node_id})
                    else:
                        await asyncio.sleep(self._host_presence_config.heartbeat_interval_seconds)
                        continue
                # We are the host monitor for `session`.
                if session.host_pending_sequence is None:
                    await self._send_host_heartbeat(session)
                    await asyncio.sleep(self._host_presence_config.heartbeat_interval_seconds)
                    continue
                # A host request is outstanding.  Wait the response timeout.
                await asyncio.sleep(self._host_presence_config.response_timeout_seconds)
                if not session.active:
                    break
                if session.host_pending_sequence is not None:
                    # No matching host ACK arrived.
                    prev_state = self._host_tracker.state
                    new_state = self._host_tracker.observe_failure()
                    if new_state == PRE_HOST and prev_state != PRE_HOST:
                        info("Host failure detected; triggering election campaign.",
                             {"nodeId": session.node_id})
                        self._trigger_election_if_eligible()
                else:
                    # A host ACK arrived while sleeping.
                    prev_state = self._host_tracker.state
                    new_state = self._host_tracker.observe_ack(session.node_id)
                    if prev_state == PRE_HOST and new_state != PRE_HOST:
                        info("Host recovered from PRE_HOST; cancelling campaign.")
                        if self._election_protocol is not None:
                            self._spawn(self._election_protocol.cancel_campaign())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._host_tracker.observe_conflict(
                f"Host monitor error: {exc}")
            if self._election_protocol is not None:
                self._spawn(self._election_protocol.cancel_campaign())

    async def _send_locked(self, session: PeerSession, payload: dict[str, Any]) -> None:
        async with session.send_lock:
            await _send(session.writer, payload)

    # ------------------------------------------------------------------
    # Peer heartbeat framing (Phase 04)
    # ------------------------------------------------------------------

    async def _send_heartbeat(self, session: PeerSession) -> None:
        """Send the next peer heartbeat with a fresh monotonic sequence."""
        session.next_sequence += 1
        sequence = session.next_sequence
        session.pending_sequence = sequence
        await self._send_locked(
            session,
            build_peer_heartbeat_payload(
                self.identity,
                session.session_id,
                sequence,
            ),
        )
        session.touch_sent(sequence)
        self._refresh_status(session)

    async def _send_heartbeat_ack(self, session: PeerSession, sequence: int) -> None:
        await self._send_locked(
        session,
        build_peer_heartbeat_ack_payload(
            self.identity,
            session.session_id,
            sequence,
        ),
    )

    # ------------------------------------------------------------------
    # Host heartbeat framing (Phase 05)
    # ------------------------------------------------------------------

    async def _send_host_heartbeat(self, session: PeerSession) -> None:
        """Send a host heartbeat to the recognised Host."""
        host_id = self._host_tracker.host_node_id
        if host_id is None:
            return
        sequence = (session.last_ack_sequence or 0) + 1000 + 1
        session.host_pending_sequence = sequence
        await self._send_locked(
            session,
            build_host_heartbeat_payload(
                self.identity,
                session.session_id,
                sequence,
                host_id,
            ),
        )
        session.touch_host_sent(sequence)

    async def _send_host_heartbeat_ack(self, session: PeerSession, sequence: int,
                                       host_node_id: str) -> None:
        await self._send_locked(
            session,
            build_host_heartbeat_ack_payload(
                self.identity,
                session.session_id,
                sequence,
                host_node_id,
            ),
        )

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _handle_session_message(self, session: PeerSession, message: dict[str, Any]) -> None:
        """Validate one authenticated session message and update presence."""
        kind = message.get("type")
        if kind not in {
            "heartbeat", "heartbeat-ack", "host-heartbeat", "host-heartbeat-ack",
            "election-request", "election-response",
            "lease-renewal-request", "lease-renewal-response",
            "agent-command", "agent-command-reply",
        }:
            session.record_error("Unsupported peer message type.")
            self._refresh_status(session)
            return
        try:
            self._validate_session_message(message, session, kind)
        except PeerProtocolError as exc:
            session.record_error(str(exc))
            self._refresh_status(session)
            return

        # Fencing invariant: observe higher term and fence local host if superseded
        term = message.get("electionTerm") or message.get("hostTerm")
        if isinstance(term, int) and self._host_lifecycle is not None:
            if term > self._host_lifecycle.term:
                self._host_lifecycle.observe_term(term)

        if kind in ("heartbeat", "heartbeat-ack"):
            await self._handle_peer_heartbeat_message(session, message, kind)
            return
        if kind in ("election-request", "election-response"):
            await self._handle_election_message(session, message, kind)
            return
        if kind in ("lease-renewal-request", "lease-renewal-response"):
            await self._handle_lease_renewal_message(session, message, kind)
            return
        if kind in ("agent-command", "agent-command-reply"):
            await self._handle_agent_command_message(session, message, kind)
            return
        await self._handle_host_heartbeat_message(session, message, kind)

    async def _handle_peer_heartbeat_message(self, session: PeerSession, message: dict[str, Any],
                                             kind: str) -> None:
        """Handle Phase 04 peer heartbeat requests and ACKs."""
        sequence = message.get("sessionSequence")
        if not isinstance(sequence, int) or sequence <= 0:
            session.record_error("Peer heartbeat sequence is invalid.")
            self._refresh_status(session)
            return
        if kind == "heartbeat":
            if sequence <= (session.last_received_sequence or 0):
                session.record_error("Duplicate or stale peer heartbeat.")
                self._refresh_status(session)
                return
            session.touch_received(sequence)
            self._refresh_status(session)
            await self._send_heartbeat_ack(session, sequence)
            return
        # heartbeat-ack
        if session.pending_sequence is None:
            session.record_error("Unexpected heartbeat ACK without a pending request.")
            self._refresh_status(session)
            return
        if sequence != session.pending_sequence:
            session.record_error("Peer heartbeat ACK does not match the pending request.")
            self._refresh_status(session)
            return
        session.touch_ack(sequence)
        self._refresh_status(session)

    async def _handle_host_heartbeat_message(self, session: PeerSession, message: dict[str, Any],
                                             kind: str) -> None:
        """Handle Phase 05 host heartbeat requests and ACKs.

        A host-heartbeat request asks the recipient to confirm it is still
        the Host.  Only the node currently tracked as Host may answer with
        a host-heartbeat-ack; any other node is a conflicting claim.
        """
        sequence = message.get("sessionSequence")
        if not isinstance(sequence, int) or sequence <= 0:
            session.record_error("Host heartbeat sequence is invalid.")
            self._refresh_status(session)
            return
        claimed_host = message.get("hostNodeId")
        if not isinstance(claimed_host, str) or not claimed_host:
            session.record_error("Host heartbeat host node id is invalid.")
            self._refresh_status(session)
            return
        if kind == "host-heartbeat":
            # The sender is asking us to confirm we are the Host.
            # We only respond with a host-heartbeat-ack if the claimed host
            # identity equals this node.  Otherwise we record a conflict.
            if claimed_host == self.identity.node_id:
                session.touch_received(sequence)
                await self._send_host_heartbeat_ack(session, sequence, claimed_host)
            else:
                self._host_tracker.observe_conflict(
                    f"Host heartbeat from {session.node_id} claims "
                    f"{claimed_host}, but this node is {self.identity.node_id}")
                if self._election_protocol is not None:
                    await self._election_protocol.cancel_campaign()
            self._refresh_status(session)
            return
        # host-heartbeat-ack
        if claimed_host != self.identity.node_id:
            # An ACK for a host identity that is not us is not valid.
            session.record_error("Host heartbeat ACK host node id does not match.")
            self._refresh_status(session)
            return
        if session.host_pending_sequence is None:
            session.record_error("Unexpected host heartbeat ACK without a pending request.")
            self._refresh_status(session)
            return
        if sequence != session.host_pending_sequence:
            session.record_error("Host heartbeat ACK does not match the pending request.")
            self._refresh_status(session)
            return
        # Valid host ACK from the recognised Host.
        session.touch_host_ack(sequence)
        prev_state = self._host_tracker.state
        new_state = self._host_tracker.observe_ack(session.node_id)
        if prev_state == PRE_HOST and new_state != PRE_HOST:
            if self._election_protocol is not None:
                await self._election_protocol.cancel_campaign()
        self._refresh_status(session)

    # ------------------------------------------------------------------
    # Election message handling (Phase 06)
    # ------------------------------------------------------------------

    async def _handle_election_message(self, session: PeerSession, message: dict[str, Any],
                                       kind: str) -> None:
        """Dispatch validated election messages to the election protocol."""
        if self._election_protocol is None:
            session.record_error("Election protocol is not configured.")
            self._refresh_status(session)
            return
        if kind == "election-request":
            await self._election_protocol.handle_vote_request(
                session.node_id, message)
        elif kind == "election-response":
            await self._election_protocol.handle_vote_response(
                session.node_id, message)
        self._refresh_status(session)

    # ------------------------------------------------------------------
    # Lease renewal & command dispatch (Phase 07)
    # ------------------------------------------------------------------

    async def _handle_lease_renewal_message(
        self, session: PeerSession, message: dict[str, Any], kind: str
    ) -> None:
        if self._election_protocol is None:
            session.record_error("Election protocol is not configured.")
            self._refresh_status(session)
            return
        if kind == "lease-renewal-request":
            await self._election_protocol.handle_lease_renewal_request(
                session.node_id, message
            )
        elif kind == "lease-renewal-response":
            await self._election_protocol.handle_lease_renewal_response(
                session.node_id, message
            )
        self._refresh_status(session)

    async def _handle_agent_command_message(
        self, session: PeerSession, message: dict[str, Any], kind: str
    ) -> None:
        if kind == "agent-command":
            await self._handle_agent_command(session, message)
        elif kind == "agent-command-reply":
            await self._handle_agent_command_reply(session, message)
        self._refresh_status(session)

    async def _handle_agent_command(self, session: PeerSession, message: dict[str, Any]) -> None:
        """Process an incoming authoritative Host command."""
        command_id = message.get("commandId", "")
        term = message.get("hostTerm")
        host_node_id = message.get("hostNodeId")
        target_node_id = message.get("targetNodeId")
        command = message.get("command")

        # 1. Message identity must match authenticated session identity
        if message.get("nodeId") != session.node_id:
            await self._send_command_reply(session, command_id, term, False, error="Session identity mismatch.")
            return

        # 2. Host Node ID in payload must match authenticated sender
        if host_node_id != session.node_id:
            await self._send_command_reply(session, command_id, term, False, error="Host identity does not match sender.")
            return

        # 3. Sender must be an authorized host peer (configured member)
        if self._election_members and session.node_id not in (m.node_id for m in self._election_members):
            await self._send_command_reply(session, command_id, term, False, error="Sender is not an election member.")
            return

        # 4. Host term must not be stale compared to local state
        known_term = 0
        if getattr(self, "_election_state_store", None) is not None:
            try:
                known_term = self._election_state_store.load().current_term
            except SecurityStoreError:
                await self._send_command_reply(session, command_id, term, False, error="Local state storage error.")
                return

        if not isinstance(term, int) or term < known_term:
            await self._send_command_reply(session, command_id, term if isinstance(term, int) else 0,
                                           False, error="Stale host term.")
            return

        # Observe term if newer
        if getattr(self, "_election_state_store", None) is not None and term > known_term:
            self._election_state_store.observe_term(term)

        # 5. Fail closed if local host tracker is in conflict
        if self._host_tracker.state == HOST_CONFLICT:
            await self._send_command_reply(session, command_id, term, False, error="Host state is in conflict.")
            return

        # 6. Target check
        if target_node_id not in (self.identity.node_id, "*"):
            await self._send_command_reply(session, command_id, term, False, error="Command not targeted to this agent.")
            return

        # 6b. Receiver-side lease-expiry validation (Phase 07 stale-command fix).
        #
        # Acceptance rule:
        #   receiver_wall_clock < leaseExpiresAt - MAX_CLOCK_SKEW_SECONDS
        #
        # Missing, non-numeric, boolean, or non-finite expiry values are rejected.
        # Note: Structurally valid timestamps do not prove clock synchronization;
        # the application does not actively verify clock offsets or NTP status.
        # Stale-command rejection is guaranteed ONLY while the explicit assumption
        # holds that the receiver's wall clock does not lag behind the Host's
        # wall clock by more than MAX_CLOCK_SKEW_SECONDS (5.0 s), and neither
        # clock is stepped backward. If receiver clock lag exceeds 5.0 s, this
        # check fails open.
        _lease_expires_at = message.get("leaseExpiresAt")
        _is_valid_expiry = (
            isinstance(_lease_expires_at, (int, float))
            and not isinstance(_lease_expires_at, bool)
            and _lease_expires_at == _lease_expires_at  # not NaN
            and _lease_expires_at != float("inf")
            and _lease_expires_at != float("-inf")
        )
        if not _is_valid_expiry:
            await self._send_command_reply(
                session, command_id, term, False,
                error="Command is missing a valid lease expiry."
            )
            return
        if self._wall_clock() >= _lease_expires_at - MAX_CLOCK_SKEW_SECONDS:
            await self._send_command_reply(
                session, command_id, term, False,
                error="Command lease has expired."
            )
            return

        # 7. Command authorization
        if command == "status":
            facts = self._agent_runtime.snapshot() if self._agent_runtime is not None else {}
            await self._send_command_reply(session, command_id, term, True, result=facts)
        elif command == "inventory":
            if self._agent_runtime is not None:
                inv = await self._agent_runtime.collect_inventory()
                await self._send_command_reply(session, command_id, term, True, result=inv)
            else:
                await self._send_command_reply(session, command_id, term, True, result={})
        else:
            await self._send_command_reply(session, command_id, term, False, error=f"Unauthorized command: {command}")

    async def _send_command_reply(
        self,
        session: PeerSession,
        command_id: str,
        term: int | None,
        success: bool,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        try:
            payload = build_agent_command_reply_payload(
                self.identity,
                session.session_id,
                command_id,
                term if isinstance(term, int) else 0,
                success,
                result=result,
                error=error,
            )
            await self._send_locked(session, payload)
        except (OSError, PeerProtocolError) as exc:
            warn("Failed to send command reply.", {"error": str(exc)})

    async def _handle_agent_command_reply(self, session: PeerSession, message: dict[str, Any]) -> None:
        command_id = message.get("commandId")
        if command_id and command_id in self._pending_command_futures:
            future = self._pending_command_futures.pop(command_id)
            if not future.done():
                future.set_result(message)

    async def send_agent_command(
        self,
        target_node_id: str,
        command: str,
        args: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Issue an authoritative command to a connected Agent peer."""
        if self._host_lifecycle is None or not self._host_lifecycle.is_authoritative():
            raise PeerProtocolError("Cannot issue command: node does not hold valid Host authority.")

        session = self._sessions.get(target_node_id)
        if session is None or not session.active:
            raise PeerProtocolError(f"No active session for target peer {target_node_id}.")

        command_id = os.urandom(8).hex()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_command_futures[command_id] = future

        # Reject if lease already invalid at send time (fail-closed).
        _wall_expiry = self._host_lifecycle.wall_clock_lease_expires_at
        if _wall_expiry <= 0 or self._wall_clock() >= _wall_expiry - MAX_CLOCK_SKEW_SECONDS:
            raise PeerProtocolError(
                "Cannot issue command: Host authority lease has already expired."
            )
        payload = build_agent_command_payload(
            self.identity,
            session.session_id,
            self._host_lifecycle.term,
            self.identity.node_id,
            target_node_id,
            command,
            command_id,
            args,
            lease_expires_at=_wall_expiry,
        )
        try:
            await self._send_locked(session, payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except Exception:
            self._pending_command_futures.pop(command_id, None)
            raise

    async def _broadcast_lease_renewal(self, term: int) -> int:
        if self._election_protocol is None:
            return 0
        return await self._election_protocol.broadcast_lease_renewal(term)

    async def _handle_election_won(self, term: int) -> None:
        if self._host_lifecycle is not None:
            now = self._clock()
            self._host_lifecycle.record_initial_lease(term, now)
            await self._host_lifecycle.activate()

    async def _send_to_peer(self, node_id: str, payload: dict[str, Any]) -> None:
        """Send a payload to a connected peer by node ID."""
        session = self._sessions.get(node_id)
        if session is None or not session.active:
            return
        await self._send_locked(session, payload)

    def _get_session_id_for(self, node_id: str) -> str | None:
        """Return the session ID for a connected peer, or None."""
        session = self._sessions.get(node_id)
        if session is None or not session.active:
            return None
        return session.session_id

    @property
    def election_protocol(self) -> ElectionProtocol | None:
        """Expose the election protocol for external coordination."""
        return self._election_protocol

    def election_snapshot(self) -> ElectionSnapshot | None:
        """Return the current election state, or None if unconfigured."""
        if self._election_protocol is None:
            return None
        return self._election_protocol.snapshot()

    def _trigger_election_if_eligible(self) -> None:
        """Trigger an election campaign if protocol prerequisites are met."""
        if self._election_protocol is not None and self._election_protocol.should_start_election():
            self._spawn(self._election_protocol.start_campaign())

    def _validate_session_message(self, message: dict[str, Any], session: PeerSession,
                                  kind: str):
        copy = dict(message)
        node_id, key = _verify(copy)
        if node_id != session.node_id or key is None:
            raise PeerProtocolError("Peer session identity is unexpected.")
        if copy.get("version") != PROTOCOL_VERSION or copy.get("network") != NETWORK_NAME:
            raise PeerProtocolError("Peer protocol is unsupported.")
        if copy.get("type") != kind:
            raise PeerProtocolError("Peer message type is unexpected.")
        if not isinstance(session.session_id, str) or len(session.session_id) < 32:
            raise PeerProtocolError("Peer session id is invalid.")
        if copy.get("sessionId") != session.session_id:
            raise PeerProtocolError("Peer session id does not match.")
        return node_id, key

    async def _shutdown_session(self, session: PeerSession) -> None:
        """Tear down one session and remove it from the registry."""
        session.close()
        self._sessions.pop(session.node_id, None)
        # Clear any pending request so a late ACK cannot be accepted.
        session.pending_sequence = None
        session.pending_since = None
        session.host_pending_sequence = None
        session.host_pending_since = None
        if self._host_session is session:
            self._host_session = None
        try:
            if session.writer.can_write_eof():
                session.writer.close()
                session.writer.transport.abort()
        except Exception:
            pass
        if session.writer is not None:
            try:
                session.writer.close()
            except Exception:
                pass