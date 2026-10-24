"""TLS configuration and direct TCP transport for peer connections."""

import asyncio
import datetime
import os
import ssl
import tempfile
from collections.abc import Awaitable, Callable

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from astral_remote.agent.network.constants import CONNECT_TIMEOUT_SECONDS

class PeerTransport:
    """Own TLS contexts, temporary certificate files, and socket creation."""

    def __init__(self) -> None:
        self._cert_files: tuple[str, str] | None = None
        self._server_context: ssl.SSLContext | None = None
        self._client_context: ssl.SSLContext | None = None

    def _ensure_certificate_files(self) -> None:
        """Create one ephemeral certificate and key for this transport lifetime."""
        if self._cert_files is not None:
            return

        cert_key = Ed25519PrivateKey.generate()
        subject = x509.Name(
            [
                x509.NameAttribute(
                    NameOID.COMMON_NAME,
                    "Astral Remote ephemeral transport",
                )
            ]
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(cert_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .sign(cert_key, algorithm=None)
        )

        cert_path: str | None = None
        key_path: str | None = None

        try:
            with tempfile.NamedTemporaryFile(
                prefix="astral-peer-",
                suffix=".pem",
                delete=False,
            ) as cert_file:
                cert_path = cert_file.name
                cert_file.write(cert.public_bytes(serialization.Encoding.PEM))

            with tempfile.NamedTemporaryFile(
                prefix="astral-peer-",
                suffix=".key",
                delete=False,
            ) as key_file:
                key_path = key_file.name
                key_file.write(
                    cert_key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )

            self._cert_files = (cert_path, key_path)

        except Exception:
            for path in (cert_path, key_path):
                if path is not None:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
            raise

    def server_ssl_context(self) -> ssl.SSLContext:
        """Return the cached TLS server context."""
        if self._server_context is None:
            self._ensure_certificate_files()
            assert self._cert_files is not None

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.load_cert_chain(*self._cert_files)
            self._server_context = context

        return self._server_context

    def client_ssl_context(self) -> ssl.SSLContext:
        """Return the cached TLS client context."""
        if self._client_context is None:
            self._ensure_certificate_files()
            assert self._cert_files is not None

            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.load_cert_chain(*self._cert_files)
            self._client_context = context

        return self._client_context

    async def start_server(
        self,
        callback: Callable[
            [asyncio.StreamReader, asyncio.StreamWriter],
            Awaitable[None],
        ],
        host: str,
        port: int | None,
    ) -> asyncio.AbstractServer:
        """Start a TLS listener and return its server handle."""
        return await asyncio.start_server(
            callback,
            host,
            port,
            ssl=self.server_ssl_context(),
        )

    async def open_connection(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open one TLS connection with a bounded connection timeout."""
        return await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=self.client_ssl_context(),
                server_hostname=None,
            ),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        """Release temporary certificate files and cached TLS contexts."""
        self._server_context = None
        self._client_context = None

        paths = self._cert_files
        self._cert_files = None

        if paths is not None:
            for path in paths:
                try:
                    os.unlink(path)
                except OSError:
                    pass