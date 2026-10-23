"""Direct TLS peer transport with Node Identity mutual authentication.

This is intentionally a small, configured mesh transport: no DHT, relay,
NAT traversal, HTTP, or WebSocket service is claimed here.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import os
import ssl
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from astral_remote.shared.security import NodeIdentity, SecurityStoreError, TrustStore, node_id_for

PROTOCOL_VERSION = 1
NETWORK_NAME = "astral-remote"
MAX_MESSAGE_BYTES = 16_384
CONNECT_TIMEOUT_SECONDS = 5


class PeerProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class BootstrapPeer:
    node_id: str
    public_key: bytes
    host: str
    port: int
    protocol_version: int = PROTOCOL_VERSION

    @classmethod
    def from_config(cls, value: dict[str, Any]) -> "BootstrapPeer":
        try:
            key = base64.b64decode(value["publicKey"], validate=True)
            node_id, host, port = value["nodeId"], value["host"], value["port"]
            version = value.get("protocolVersion", PROTOCOL_VERSION)
            if not isinstance(host, str) or not host or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError
            if not isinstance(version, int) or version != PROTOCOL_VERSION or node_id != node_id_for(key):
                raise ValueError
            return cls(node_id, key, host, port, version)
        except (KeyError, TypeError, ValueError, SecurityStoreError) as exc:
            raise PeerProtocolError("Bootstrap peer configuration is invalid.") from exc


@dataclass(frozen=True)
class PeerStatus:
    node_id: str
    connected: bool
    transport: str
    error: str | None = None


def parse_network_config(config: dict[str, Any] | None) -> tuple[bool, str, int | None, tuple[BootstrapPeer, ...]]:
    raw = (config or {}).get("peerNetwork", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise PeerProtocolError("Peer network configuration is invalid.")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PeerProtocolError("Peer network enabled must be true or false.")
    bind_host, bind_port = raw.get("bindHost", "127.0.0.1"), raw.get("bindPort")
    if not isinstance(bind_host, str) or not bind_host:
        raise PeerProtocolError("Peer network bind host is invalid.")
    if bind_port is not None and (not isinstance(bind_port, int) or not 0 <= bind_port <= 65535):
        raise PeerProtocolError("Peer network bind port is invalid.")
    peers = raw.get("bootstrapPeers", [])
    if not isinstance(peers, list):
        raise PeerProtocolError("Bootstrap peer list is invalid.")
    return enabled, bind_host, bind_port, tuple(BootstrapPeer.from_config(item) for item in peers)


def _canon(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


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


async def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
    data = _canon(message)
    if len(data) > MAX_MESSAGE_BYTES:
        raise PeerProtocolError("Peer message exceeds size limit.")
    writer.write(len(data).to_bytes(4, "big") + data)
    await writer.drain()


async def _receive(reader: asyncio.StreamReader) -> dict[str, Any]:
    length = int.from_bytes(await reader.readexactly(4), "big")
    if not 0 < length <= MAX_MESSAGE_BYTES:
        raise PeerProtocolError("Peer message exceeds size limit.")
    try:
        value = json.loads(await reader.readexactly(length))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except Exception as exc:
        raise PeerProtocolError("Peer message is malformed.") from exc


class PeerNetwork:
    """Agent-owned direct TLS listener and configured bootstrap connections."""
    def __init__(self, identity: NodeIdentity, trust_store: TrustStore, config: dict[str, Any] | None = None):
        self.identity, self.trust_store = identity, trust_store
        self.enabled, self.bind_host, self.bind_port, self.bootstraps = parse_network_config(config)
        self._server: asyncio.AbstractServer | None = None
        self._peers: dict[str, PeerStatus] = {}
        self._tasks: set[asyncio.Task] = set()
        self._cert_files: tuple[str, str] | None = None

    @property
    def listening_port(self) -> int | None:
        if self._server is None or not self._server.sockets:
            return None
        return int(self._server.sockets[0].getsockname()[1])

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "listening": self._server is not None, "bindHost": self.bind_host,
                "port": self.listening_port, "protocolVersion": PROTOCOL_VERSION,
                "peers": [status.__dict__.copy() for status in self._peers.values()]}

    async def start(self) -> None:
        if not self.enabled or self._server is not None:
            return
        context = self._ssl_context()
        self._server = await asyncio.start_server(self._accept, self.bind_host, self.bind_port, ssl=context)
        for peer in self.bootstraps:
            self._spawn(self.connect(peer))

    async def close(self) -> None:
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._cert_files:
            for name in self._cert_files:
                try: os.unlink(name)
                except OSError: pass
            self._cert_files = None

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
        cert_file.write(cert.public_bytes(serialization.Encoding.PEM)); cert_file.close()
        key_file.write(cert_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())); key_file.close()
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
        if not self.enabled or self._peers.get(peer.node_id, PeerStatus("", False, "")).connected:
            return False
        if not self.trust_store.is_trusted(peer.node_id, peer.public_key):
            self._peers[peer.node_id] = PeerStatus(peer.node_id, False, "direct-tls", "Bootstrap peer is not trusted.")
            return False
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(peer.host, peer.port, ssl=self._client_ssl_context(), server_hostname=None), CONNECT_TIMEOUT_SECONDS)
            await self._authenticate(reader, writer, initiator=True, expected=peer)
            self._peers[peer.node_id] = PeerStatus(peer.node_id, True, "direct-tls")
            return True
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, PeerProtocolError, SecurityStoreError) as exc:
            self._peers[peer.node_id] = PeerStatus(peer.node_id, False, "direct-tls", str(exc))
            return False
        finally:
            if 'writer' in locals():
                writer.close(); await writer.wait_closed()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            node_id = await self._authenticate(reader, writer, initiator=False)
            self._peers[node_id] = PeerStatus(node_id, True, "direct-tls")
        except (PeerProtocolError, SecurityStoreError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            try: await writer.wait_closed()
            except OSError: pass

    async def _authenticate(self, reader, writer, *, initiator: bool, expected: BootstrapPeer | None = None):
        local_nonce = base64.b64encode(os.urandom(32)).decode("ascii")
        if initiator:
            await _send(writer, self._hello("client-hello", local_nonce, None))
            remote = await _receive(reader)
            node_id, key, remote_nonce = self._validate_hello(remote, "server-hello", local_nonce)
            self._check_peer(node_id, key, expected)
            await _send(writer, self._hello("client-finish", local_nonce, remote_nonce))
            return node_id
        remote = await _receive(reader)
        node_id, key, remote_nonce = self._validate_hello(remote, "client-hello", None)
        self._check_peer(node_id, key, None)
        server_nonce = os.urandom(32).hex()
        await _send(writer, self._hello("server-hello", server_nonce, remote_nonce))
        finish = await _receive(reader)
        finish_id, finish_key, _ = self._validate_hello(finish, "client-finish", server_nonce)
        if finish_id != node_id or finish_key != key:
            raise PeerProtocolError("Peer authentication identity changed.")
        return node_id

    def _hello(self, kind: str, nonce: str, peer_nonce: str | None) -> dict[str, Any]:
        value = {"version": PROTOCOL_VERSION, "network": NETWORK_NAME, "type": kind, "nodeId": self.identity.node_id,
                 "publicKey": base64.b64encode(self.identity.public_key).decode("ascii"), "nonce": nonce}
        if peer_nonce is not None: value["peerNonce"] = peer_nonce
        value["signature"] = _sign(self.identity, value)
        return value

    def _validate_hello(self, message: dict[str, Any], kind: str, peer_nonce: str | None):
        copy = dict(message)
        node_id, key = _verify(copy)
        if copy.get("version") != PROTOCOL_VERSION or copy.get("network") != NETWORK_NAME or copy.get("type") != kind:
            raise PeerProtocolError("Peer protocol is unsupported.")
        nonce = copy.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 32 or (peer_nonce is not None and copy.get("peerNonce") != peer_nonce):
            raise PeerProtocolError("Peer authentication nonce is invalid.")
        return node_id, key, nonce

    def _check_peer(self, node_id: str, key: bytes, expected: BootstrapPeer | None) -> None:
        if expected and (node_id != expected.node_id or key != expected.public_key):
            raise PeerProtocolError("Bootstrap peer identity does not match.")
        if not self.trust_store.is_trusted(node_id, key):
            raise PeerProtocolError("Peer is not trusted.")
