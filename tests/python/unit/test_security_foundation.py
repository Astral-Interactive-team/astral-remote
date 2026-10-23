import base64

import pytest

from astral_remote.agent.identity import StateStore
from astral_remote.shared.security import (
    CredentialStore, IdentityStore, SecurityStoreError, TrustStore, XorTestProtector, node_id_for,
)


def test_node_identity_persists_without_changing_installation_id(tmp_path):
    protector = XorTestProtector(b"test-key")
    identity_path = tmp_path / "identity.json"
    installation = StateStore(identity_path).installation_id
    first = IdentityStore(tmp_path / "node.bin", protector).load_or_create()
    second = IdentityStore(tmp_path / "node.bin", protector).load_or_create()
    assert first.node_id == second.node_id
    assert first.public_key == second.public_key
    assert StateStore(identity_path).installation_id == installation
    assert b"privateKey" not in (tmp_path / "node.bin").read_bytes()
    assert first.node_id.startswith("node-v1-")


def test_corrupt_identity_and_invalid_trust_are_rejected(tmp_path):
    protector = XorTestProtector(b"test-key")
    path = tmp_path / "node.bin"
    path.write_bytes(b"broken")
    with pytest.raises(SecurityStoreError):
        IdentityStore(path, protector).load_or_create()
    trust = TrustStore(tmp_path / "trust.json")
    with pytest.raises(SecurityStoreError):
        trust.trust("node-v1-bad", b"bad")


def test_trust_and_protected_credentials_lifecycle(tmp_path):
    protector = XorTestProtector(b"test-key")
    node = IdentityStore(tmp_path / "node.bin", protector).load_or_create()
    trust = TrustStore(tmp_path / "trust.json")
    trust.trust(node.node_id, node.public_key)
    assert trust.is_trusted(node.node_id, node.public_key) is True
    trust.revoke(node.node_id)
    assert trust.is_trusted(node.node_id, node.public_key) is False
    credentials = CredentialStore(tmp_path / "host.bin", protector)
    assert credentials.has_token() is False
    credentials.save_token("abc.def.0123456789")
    assert credentials.load_token() == "abc.def.0123456789"
    assert b"abc.def" not in (tmp_path / "host.bin").read_bytes()
    credentials.clear()
    assert credentials.has_token() is False


def test_unsupported_versions_and_unknown_nodes_fail_closed(tmp_path):
    protector = XorTestProtector(b"test-key")
    path = tmp_path / "node.bin"
    IdentityStore(path, protector).load_or_create()
    outer = path.read_text(encoding="utf-8").replace('"version":1', '"version":99', 1)
    path.write_text(outer, encoding="utf-8")
    with pytest.raises(SecurityStoreError, match="version"):
        IdentityStore(path, protector).load_or_create()
    trust = TrustStore(tmp_path / "trust.json")
    assert trust.get("node-v1-" + "0" * 64) is None
    assert trust.revoke("node-v1-" + "0" * 64) is False


def test_protected_store_never_returns_token_on_decryption_failure(tmp_path):
    path = tmp_path / "host.bin"
    CredentialStore(path, XorTestProtector(b"one")).save_token("abc.def.0123456789")
    with pytest.raises(SecurityStoreError):
        CredentialStore(path, XorTestProtector(b"two")).load_token()
