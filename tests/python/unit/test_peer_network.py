import asyncio
import base64

import pytest

from astral_remote.agent.peer_network import BootstrapPeer, PeerNetwork, PeerProtocolError, parse_network_config
from astral_remote.shared.security import IdentityStore, TrustStore, XorTestProtector


def _identity(path, key):
    return IdentityStore(path, XorTestProtector(key)).load_or_create()


def _config(peer=None):
    raw = {"peerNetwork": {"enabled": True, "bindHost": "127.0.0.1", "bindPort": 0, "bootstrapPeers": []}}
    if peer:
        raw["peerNetwork"]["bootstrapPeers"] = [{"nodeId": peer.node_id, "publicKey": base64.b64encode(peer.public_key).decode(), "host": "127.0.0.1", "port": peer.port, "protocolVersion": 1}]
    return raw


def test_network_config_rejects_bad_bootstrap_key():
    with pytest.raises(PeerProtocolError):
        parse_network_config({"peerNetwork": {"enabled": True, "bootstrapPeers": [{"nodeId": "bad"}]}})


def test_direct_tls_connection_requires_mutual_trust(tmp_path):
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server_trust, client_trust = TrustStore(tmp_path / "server-trust"), TrustStore(tmp_path / "client-trust")
        server_trust.trust(client_identity.node_id, client_identity.public_key)
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        server = PeerNetwork(server_identity, server_trust, _config())
        await server.start()
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer))
        await client.start()
        for _ in range(50):
            if (client.status()["peers"] and client.status()["peers"][0]["connected"]
                    and server.status()["peers"]):
                break
            await asyncio.sleep(.02)
        assert client.status()["peers"][0]["connected"] is True
        assert server.status()["peers"][0]["node_id"] == client_identity.node_id
        await client.close(); await server.close()
    asyncio.run(scenario())


def test_untrusted_peer_is_not_connected(tmp_path):
    async def scenario():
        server_identity, client_identity = _identity(tmp_path / "server", b"server"), _identity(tmp_path / "client", b"client")
        server = PeerNetwork(server_identity, TrustStore(tmp_path / "server-trust"), _config())
        await server.start()
        client_trust = TrustStore(tmp_path / "client-trust")
        client_trust.trust(server_identity.node_id, server_identity.public_key)
        peer = BootstrapPeer(server_identity.node_id, server_identity.public_key, "127.0.0.1", server.listening_port)
        client = PeerNetwork(client_identity, client_trust, _config(peer))
        await client.start(); await asyncio.sleep(.05)
        assert client.status()["peers"][0]["connected"] is False
        await client.close(); await server.close()
    asyncio.run(scenario())
