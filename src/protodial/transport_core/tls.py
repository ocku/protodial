"""TLS material loading and validation."""

from __future__ import annotations

import pathlib
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from ..errors import LocalFileError, TlsMaterialError, TlsMaterialSource
from .options import transport_config, transport_mode


class PrivateKey(Protocol):
    """Serialize a loaded private key."""

    def private_bytes(
        self,
        encoding: Encoding,
        format: PrivateFormat,
        encryption_algorithm: object,
    ) -> bytes:
        """Serialize the key with the requested encoding and format."""
        ...


PrivateKeyLoader = Callable[[bytes, bytes | None], PrivateKey]
SslContextFactory = Callable[[int], ssl.SSLContext]
DefaultContextFactory = Callable[[], ssl.SSLContext]


@dataclass(frozen=True)
class TlsMaterial:
    """Validated certificate and private-key bytes for a channel."""

    root_certificates: bytes | None
    certificate_chain: bytes | None
    private_key: bytes | None


@dataclass(frozen=True)
class _TlsOptions:
    """TLS-related command values needed by material validation."""

    mode: str
    ca_paths: tuple[str, ...]
    certificate_path: str | None
    private_key_path: str | None
    private_key_password: str | None
    server_name: str | None
    authority: str | None

    @classmethod
    def from_command(cls, command_or_transport: object) -> _TlsOptions:
        """Read TLS options from either a command or transport object."""
        transport = transport_config(command_or_transport)
        return cls(
            mode=transport_mode(transport),
            ca_paths=ca_paths(transport),
            certificate_path=getattr(transport, "cert", None),
            private_key_path=getattr(transport, "key", None),
            private_key_password=getattr(transport, "key_password", None),
            server_name=getattr(transport, "server_name", None),
            authority=getattr(transport, "authority", None),
        )

    def validate_combinations(self) -> None:
        """Reject conflicting or incomplete TLS option combinations."""
        if bool(self.certificate_path) != bool(self.private_key_path):
            raise ValueError(
                "client certificate and private key must be used together"
            )
        if self.private_key_password is not None and not self.private_key_path:
            raise ValueError(
                "client private key password requires a private key"
            )
        if self.server_name == "":
            raise ValueError("TLS server name must not be empty")
        if self.authority == "":
            raise ValueError("HTTP/2 authority must not be empty")
        if self.mode == "plaintext" and self._uses_tls_options():
            raise ValueError(
                "TLS credential and server-name options cannot be used with "
                "plaintext"
            )
        if self.mode == "insecure" and self.ca_paths:
            raise ValueError("CA certificates cannot be used with insecure TLS")

    def _uses_tls_options(self) -> bool:
        """Return whether an option requiring TLS was configured."""
        return bool(
            self.ca_paths
            or self.certificate_path
            or self.private_key_path
            or self.private_key_password is not None
            or self.server_name
        )


def ca_paths(transport: object) -> tuple[str, ...]:
    """Return the configured CA certificate paths."""
    return tuple(getattr(transport, "ca_paths", ()))


def read_nonempty(
    path: str,
    description: str,
    argument: str,
    *,
    occurrence: int = 0,
) -> bytes:
    """Read a required nonempty binary file."""
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError as error:
        raise LocalFileError(
            description,
            path,
            argument,
            error,
            occurrence=occurrence,
        ) from error
    if not data.strip():
        source = TlsMaterialSource(
            description,
            path,
            argument,
            occurrence=occurrence,
        )
        raise TlsMaterialError(
            f"{description} `{path}` is empty",
            sources=(source,),
            reason="the file contains no non-whitespace data",
            help=(
                f"write valid PEM data to `{path}`, then pass it with "
                f"`{argument}`"
            ),
            kind="empty",
        )
    return data


def _load_ca_roots(
    paths: tuple[str, ...],
    default_context_factory: DefaultContextFactory,
) -> bytes | None:
    """Read and validate a repeatable CA certificate option."""
    if not paths:
        return None
    path_occurrences: dict[str, int] = {}
    loaded_roots: list[tuple[str, int, bytes]] = []
    for path in paths:
        occurrence = path_occurrences.get(path, 0)
        path_occurrences[path] = occurrence + 1
        data = read_nonempty(
            path,
            "CA certificate file",
            "--cacert",
            occurrence=occurrence,
        )
        loaded_roots.append((path, occurrence, data))

    root_parts: list[bytes] = []
    for path, occurrence, data in loaded_roots:
        _validate_ca_root(
            path,
            occurrence,
            data,
            default_context_factory,
        )
        root_parts.append(data.rstrip() + b"\n")
    return b"".join(root_parts)


def _validate_ca_root(
    path: str,
    occurrence: int,
    data: bytes,
    default_context_factory: DefaultContextFactory,
) -> None:
    """Validate one PEM CA certificate bundle with the TLS library."""
    try:
        default_context_factory().load_verify_locations(
            cadata=data.decode("ascii")
        )
    except (UnicodeDecodeError, ssl.SSLError) as error:
        source = TlsMaterialSource(
            "CA certificate file",
            path,
            "--cacert",
            occurrence=occurrence,
        )
        raise TlsMaterialError(
            f"CA certificate file `{path}` is not a valid PEM "
            "certificate bundle",
            sources=(source,),
            reason=str(error) or "the certificate parser rejected it",
            help=(
                "provide a PEM-encoded CA certificate bundle with `--cacert`"
            ),
            kind="invalid_ca",
        ) from error


def _identity_sources(
    certificate_path: str, private_key_path: str
) -> tuple[TlsMaterialSource, TlsMaterialSource]:
    """Describe the two files forming a client TLS identity."""
    return (
        TlsMaterialSource(
            "client certificate file", certificate_path, "--cert", -1
        ),
        TlsMaterialSource(
            "client private key file", private_key_path, "--key", -1
        ),
    )


def _validate_client_identity(
    certificate_path: str,
    private_key_path: str,
    password: str | None,
    ssl_context_factory: SslContextFactory,
) -> None:
    """Check that client certificate and key files form a TLS identity."""
    validation_context = ssl_context_factory(ssl.PROTOCOL_TLS_CLIENT)
    try:
        validation_context.load_cert_chain(
            certfile=certificate_path,
            keyfile=private_key_path,
            password=password if password is not None else b"",
        )
    except (OSError, ssl.SSLError) as error:
        raise TlsMaterialError(
            "client certificate and private key are invalid or do not match",
            sources=_identity_sources(certificate_path, private_key_path),
            reason=str(error) or "the TLS library rejected the identity",
            help=(
                "provide a matching PEM certificate and private key with "
                "`--cert` and `--key`"
            ),
            kind="invalid_identity",
        ) from error


def _decrypt_private_key(
    private_key: bytes,
    password: str,
    path: str,
    private_key_loader: PrivateKeyLoader,
) -> bytes:
    """Decrypt and normalize a password-protected private key for grpc."""
    try:
        key = private_key_loader(private_key, password.encode("utf-8"))
        return key.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
            NoEncryption(),
        )
    except (TypeError, ValueError, UnsupportedAlgorithm) as error:
        source = TlsMaterialSource("client private key file", path, "--key", -1)
        raise TlsMaterialError(
            f"could not decrypt client private key `{path}`",
            sources=(source,),
            reason=str(error) or "the private key could not be decoded",
            help=(
                "check `--key-password` and provide a supported PEM private key"
            ),
            kind="invalid_private_key",
        ) from error


def _load_client_identity(
    options: _TlsOptions,
    ssl_context_factory: SslContextFactory,
    private_key_loader: PrivateKeyLoader,
) -> tuple[bytes | None, bytes | None]:
    """Read, validate, and when needed decrypt the client identity."""
    certificate_path = options.certificate_path
    private_key_path = options.private_key_path
    if certificate_path is None or private_key_path is None:
        return None, None
    certificate_chain = read_nonempty(
        certificate_path,
        "client certificate file",
        "--cert",
        occurrence=-1,
    )
    private_key = read_nonempty(
        private_key_path,
        "client private key file",
        "--key",
        occurrence=-1,
    )
    _validate_client_identity(
        certificate_path,
        private_key_path,
        options.private_key_password,
        ssl_context_factory,
    )
    if options.private_key_password is not None:
        private_key = _decrypt_private_key(
            private_key,
            options.private_key_password,
            private_key_path,
            private_key_loader,
        )
    return certificate_chain, private_key


def validate_tls_material(
    command_or_transport: object,
    *,
    ssl_context_factory: SslContextFactory,
    default_context_factory: DefaultContextFactory,
    private_key_loader: PrivateKeyLoader,
) -> TlsMaterial:
    """Read and validate configured TLS files before grpc opens a channel."""
    options = _TlsOptions.from_command(command_or_transport)
    options.validate_combinations()
    roots = _load_ca_roots(options.ca_paths, default_context_factory)
    certificate_chain, private_key = _load_client_identity(
        options,
        ssl_context_factory,
        private_key_loader,
    )
    return TlsMaterial(roots, certificate_chain, private_key)
