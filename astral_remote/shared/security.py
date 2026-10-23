"""Versioned local security stores. No network work or secret logging."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

STORE_VERSION = 1
IDENTITY_VERSION = 1
TRUST_VERSION = 1


class SecurityStoreError(RuntimeError):
    """Safe local-store error; never includes protected data."""


class DataProtector(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class XorTestProtector:
    """Test fixture only. Production defaults never select it."""
    def __init__(self, key: bytes):
        self._key = bytes(key)
        if not self._key:
            raise ValueError("Test protector key is empty.")

    def protect(self, data: bytes) -> bytes:
        nonce = secrets.token_bytes(16)
        stream = hashlib.sha256(self._key + nonce).digest()
        return nonce + bytes(value ^ stream[index % len(stream)] for index, value in enumerate(data))

    def unprotect(self, data: bytes) -> bytes:
        if len(data) < 17:
            raise SecurityStoreError("Protected data is malformed.")
        nonce, body = data[:16], data[16:]
        stream = hashlib.sha256(self._key + nonce).digest()
        return bytes(value ^ stream[index % len(stream)] for index, value in enumerate(body))


class WindowsDpapiProtector:
    """Windows DPAPI current-user scope; not defense against compromised user session."""
    def _crypt(self, data: bytes, protect: bool) -> bytes:
        if os.name != "nt":
            raise SecurityStoreError("DPAPI is available only on Windows.")
        import ctypes
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

        source = (ctypes.c_byte * len(data)).from_buffer_copy(data)
        incoming, outgoing = DataBlob(len(data), source), DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(DataBlob), wintypes.LPCWSTR, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
        function.restype = wintypes.BOOL
        if not function(ctypes.byref(incoming), None, None, None, None, 0, ctypes.byref(outgoing)):
            raise SecurityStoreError("DPAPI protection failed." if protect else "DPAPI decryption failed.")
        try:
            return bytes(ctypes.string_at(outgoing.pbData, outgoing.cbData))
        finally:
            kernel32.LocalFree(outgoing.pbData)

    def protect(self, data: bytes) -> bytes:
        return self._crypt(data, True)

    def unprotect(self, data: bytes) -> bytes:
        return self._crypt(data, False)


def _local_path(name: str) -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise SecurityStoreError("Local credential directory is unavailable.")
    return Path(base) / "AstralRemote" / name


class _ProtectedFile:
    def __init__(self, path: str | Path, protector: DataProtector):
        self.path, self.protector = Path(path), protector

    def load(self, kind: str) -> dict | None:
        if not self.path.exists():
            return None
        try:
            outer = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(outer, dict) or outer.get("version") != STORE_VERSION or outer.get("kind") != kind:
                raise SecurityStoreError("Protected store version is unsupported.")
            encoded = outer.get("data")
            if not isinstance(encoded, str):
                raise SecurityStoreError("Protected store data is malformed.")
            value = json.loads(self.protector.unprotect(base64.b64decode(encoded, validate=True)))
            if not isinstance(value, dict):
                raise SecurityStoreError("Protected store data is malformed.")
            return value
        except SecurityStoreError:
            raise
        except Exception as exc:
            raise SecurityStoreError("Protected store cannot be read.") from exc

    def save(self, kind: str, value: dict) -> None:
        try:
            raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            outer = {"version": STORE_VERSION, "kind": kind,
                     "data": base64.b64encode(self.protector.protect(raw)).decode("ascii")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(outer, separators=(",", ":")), encoding="utf-8")
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
        except SecurityStoreError:
            raise
        except Exception as exc:
            raise SecurityStoreError("Protected store cannot be saved.") from exc

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            raise SecurityStoreError("Protected store cannot be removed.") from exc


@dataclass(frozen=True)
class NodeIdentity:
    version: int
    algorithm: str
    node_id: str
    public_key: bytes
    private_key: bytes


def node_id_for(public_key: bytes) -> str:
    if not isinstance(public_key, bytes):
        raise SecurityStoreError("Node public key is invalid.")
    try:
        Ed25519PublicKey.from_public_bytes(public_key)
    except Exception as exc:
        raise SecurityStoreError("Node public key is invalid.") from exc
    return "node-v1-" + hashlib.sha256(public_key).hexdigest()


class IdentityStore:
    """DPAPI-protected Ed25519 key store; corrupt data is never reset."""
    def __init__(self, path: str | Path | None = None, protector: DataProtector | None = None):
        self._file = _ProtectedFile(path or _local_path("node-identity.json"), protector or WindowsDpapiProtector())

    def load_or_create(self) -> NodeIdentity:
        data = self._file.load("node-identity")
        if data is None:
            private = Ed25519PrivateKey.generate()
            raw_private = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
            raw_public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            self._file.save("node-identity", {"version": IDENTITY_VERSION, "algorithm": "Ed25519",
                                                "private": base64.b64encode(raw_private).decode("ascii"),
                                                "public": base64.b64encode(raw_public).decode("ascii")})
            data = self._file.load("node-identity")
            if data is None:
                raise SecurityStoreError("Node identity was not saved.")
        try:
            if data.get("version") != IDENTITY_VERSION or data.get("algorithm") != "Ed25519":
                raise SecurityStoreError("Node identity version is unsupported.")
            private, public = base64.b64decode(data["private"], validate=True), base64.b64decode(data["public"], validate=True)
            derived = Ed25519PrivateKey.from_private_bytes(private).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            if derived != public:
                raise SecurityStoreError("Node identity key pair does not match.")
            return NodeIdentity(IDENTITY_VERSION, "Ed25519", node_id_for(public), public, private)
        except SecurityStoreError:
            raise
        except Exception as exc:
            raise SecurityStoreError("Node identity is malformed.") from exc


class HostCredentialStore:
    """DPAPI-protected token store for explicitly configured Host candidates."""
    def __init__(self, path: str | Path | None = None, protector: DataProtector | None = None):
        self._file = _ProtectedFile(path or _local_path("host-credential.json"), protector or WindowsDpapiProtector())

    def has_token(self) -> bool:
        return self._file.path.exists()

    def save_token(self, token: str) -> None:
        if not isinstance(token, str) or not token.strip():
            raise SecurityStoreError("Host credential is empty.")
        self._file.save("host-credential", {"version": 1, "token": token.strip()})

    def load_token(self) -> str:
        data = self._file.load("host-credential")
        if data is None or data.get("version") != 1 or not isinstance(data.get("token"), str) or not data["token"]:
            raise SecurityStoreError("Host credential is unavailable.")
        return data["token"]

    def clear(self) -> None:
        self._file.clear()


CredentialStore = HostCredentialStore


@dataclass(frozen=True)
class TrustRecord:
    node_id: str
    public_key: bytes
    state: str


class TrustStore:
    """Local trust policy. Enrollment and handshakes remain future work."""
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": TRUST_VERSION, "nodes": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("version") != TRUST_VERSION or not isinstance(value.get("nodes"), dict):
                raise ValueError
            return value
        except Exception as exc:
            raise SecurityStoreError("Trust store cannot be read.") from exc

    def _write(self, value: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            os.replace(temp, self.path)
        except OSError as exc:
            raise SecurityStoreError("Trust store cannot be saved.") from exc

    def trust(self, node_id: str, public_key: bytes) -> TrustRecord:
        if not isinstance(node_id, str) or node_id != node_id_for(public_key):
            raise SecurityStoreError("Node identity does not match its public key.")
        data = self._read()
        data["nodes"][node_id] = {"public": base64.b64encode(public_key).decode("ascii"), "state": "trusted"}
        self._write(data)
        return TrustRecord(node_id, public_key, "trusted")

    def get(self, node_id: str) -> TrustRecord | None:
        entry = self._read()["nodes"].get(node_id)
        if entry is None:
            return None
        try:
            public = base64.b64decode(entry["public"], validate=True)
            state = entry["state"]
            if state not in ("trusted", "revoked") or node_id != node_id_for(public):
                raise ValueError
            return TrustRecord(node_id, public, state)
        except Exception as exc:
            raise SecurityStoreError("Trust store contains invalid node data.") from exc

    def revoke(self, node_id: str) -> bool:
        data = self._read()
        if node_id not in data["nodes"]:
            return False
        data["nodes"][node_id]["state"] = "revoked"
        self._write(data)
        return True

    def is_trusted(self, node_id: str, public_key: bytes) -> bool:
        record = self.get(node_id)
        return bool(record and record.state == "trusted" and record.public_key == public_key)
