"""Cryptographic message signing and verification for peer networking."""

import base64
from typing import Any
import os

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from astral_remote.agent.network.codec import _canon, _receive, _send
from astral_remote.agent.network.constants import NETWORK_NAME, PROTOCOL_VERSION
from astral_remote.agent.network.models import BootstrapPeer
from astral_remote.agent.network.errors import PeerProtocolError
from astral_remote.shared.security import NodeIdentity, node_id_for

def _sign(identity: NodeIdentity, message: dict[str, Any]) -> str:
    return base64.b64encode(Ed25519PrivateKey.from_private_bytes(identity.private_key).sign(_canon(message))).decode("ascii")


def _verify(message: dict[str, Any]) -> tuple[str, bytes]:
    try:
        signature = base64.b64decode(message.pop("signature"), validate=True)
        public = base64.b64decode(message["publicKey"], validate=True)
        node_id = message["nodeId"]
        if node_id != node_id_for(public):
            raise ValueError
        Ed25519PublicKey.from_public_bytes(public).verify(signature, _canon(message))
        return node_id, public
    except Exception as exc:
        raise PeerProtocolError("Peer authentication signature is invalid.") from exc

class PeerAuthenticator:
    def __init__(self, identity: NodeIdentity, trust_store) -> None:
        self.identity = identity
        self.trust_store = trust_store

    def _hello(
        self,
        kind: str,
        nonce: str,
        peer_nonce: str | None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        value = {
            "version": PROTOCOL_VERSION,
            "network": NETWORK_NAME,
            "type": kind,
            "nodeId": self.identity.node_id,
            "publicKey": base64.b64encode(
                self.identity.public_key
            ).decode("ascii"),
            "nonce": nonce,
        }

        if peer_nonce is not None:
            value["peerNonce"] = peer_nonce
        if session_id is not None:
            value["sessionId"] = session_id

        value["signature"] = _sign(self.identity, value)
        return value
    
    def _validate_hello(
        self,
        message: dict[str, Any],
        kind: str,
        peer_nonce: str | None,
        session_id: str | None = None,
    ):
        copy = dict(message)
        node_id, key = _verify(copy)

        if (
            copy.get("version") != PROTOCOL_VERSION
            or copy.get("network") != NETWORK_NAME
            or copy.get("type") != kind
        ):
            raise PeerProtocolError("Peer protocol is unsupported.")

        nonce = copy.get("nonce")
        if (
            not isinstance(nonce, str)
            or len(nonce) < 32
            or (
                peer_nonce is not None
                and copy.get("peerNonce") != peer_nonce
            )
        ):
            raise PeerProtocolError("Peer authentication nonce is invalid.")

        remote_session = copy.get("sessionId")
        if session_id is not None and remote_session != session_id:
            raise PeerProtocolError(
                "Peer authentication session does not match."
            )

        if (
            session_id is None
            and remote_session is not None
            and not isinstance(remote_session, str)
        ):
            raise PeerProtocolError(
                "Peer authentication session id is invalid."
            )

        return node_id, key, nonce, remote_session
    
    async def authenticate(
        self,
        reader,
        writer,
        *,
        initiator: bool,
        expected: BootstrapPeer | None = None,
        session_id: str | None = None,
    ) -> tuple[str, str]:
        local_nonce = base64.b64encode(
            os.urandom(32)
        ).decode("ascii")

        if initiator:
            await _send(
                writer,
                self._hello(
                    "client-hello", local_nonce, None, session_id
                ),
            )

            remote = await _receive(reader)
            node_id, key, remote_nonce, remote_session = (
                self._validate_hello(
                    remote,
                    "server-hello",
                    local_nonce,
                    session_id,
                )
            )

            self._check_peer(node_id, key, expected)

            await _send(
                writer,
                self._hello(
                    "client-finish",
                    local_nonce,
                    remote_nonce,
                    session_id,
                ),
            )

            return node_id, session_id

        remote = await _receive(reader)
        node_id, key, remote_nonce, session_id = self._validate_hello(
            remote,
            "client-hello",
            None,
            session_id,
        )

        self._check_peer(node_id, key, None)

        server_nonce = os.urandom(32).hex()
        await _send(
            writer,
            self._hello(
                "server-hello",
                server_nonce,
                remote_nonce,
                session_id,
            ),
        )

        finish = await _receive(reader)
        finish_id, finish_key, _, finish_session = (
            self._validate_hello(
                finish,
                "client-finish",
                server_nonce,
                session_id,
            )
        )

        if (
            finish_id != node_id
            or finish_key != key
            or finish_session != session_id
        ):
            raise PeerProtocolError(
                "Peer authentication identity changed."
            )

        return node_id, session_id
    
    def _check_peer(
        self,
        node_id: str,
        key: bytes,
        expected: BootstrapPeer | None,
    ) -> None:
        if expected is not None and (
            node_id != expected.node_id
            or key != expected.public_key
        ):
            raise PeerProtocolError(
                "Bootstrap peer identity does not match."
            )

        if not self.trust_store.is_trusted(node_id, key):
            raise PeerProtocolError("Peer is not trusted.")