"""Bounded live-reflection source construction for completion."""

from __future__ import annotations

import argparse
import math
import os
import stat
import tempfile
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import NoReturn, cast

from ..schema.contracts import DescriptorSource, ReflectionChannel
from ._arguments import Scan
from ._selection import reflection_endpoint

_MAX_TLS_FILE_BYTES = 1024 * 1024
_MAX_TLS_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_TLS_FILES = 16
_DEFAULT_COMPLETION_TIMEOUT = 0.2


def _number(scan: Scan, name: str, default: float) -> float:
    value = scan.last(name)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def _integer(scan: Scan, name: str) -> int | None:
    value = scan.last(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _completion_timeout(environment: Mapping[str, str]) -> float:
    raw = environment.get(
        "PROTODIAL_COMPLETION_TIMEOUT", str(_DEFAULT_COMPLETION_TIMEOUT)
    )
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_COMPLETION_TIMEOUT
    return (
        value
        if math.isfinite(value) and value > 0
        else _DEFAULT_COMPLETION_TIMEOUT
    )


def _bounded_timeout(
    scan: Scan,
    name: str,
    environment: Mapping[str, str],
) -> float:
    cap = _completion_timeout(environment)
    requested = _number(scan, name, cap)
    if not math.isfinite(requested) or requested <= 0:
        return cap
    return min(requested, cap)


def _read_tls_file(path: str, deadline: float) -> bytes | None:
    """Read one TLS input once without blocking on special files."""
    descriptor: int | None = None
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(resolved, flags)
        details = os.fstat(descriptor)
        if (
            time.monotonic() >= deadline
            or not stat.S_ISREG(details.st_mode)
            or details.st_size > _MAX_TLS_FILE_BYTES
        ):
            return None
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            data = source.read(_MAX_TLS_FILE_BYTES + 1)
        return data if len(data) <= _MAX_TLS_FILE_BYTES else None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _snapshot_tls_files(
    ca_paths: tuple[str, ...],
    cert: str | None,
    key: str | None,
    deadline: float,
    resources: ExitStack,
) -> tuple[tuple[str, ...], str | None, str | None] | None:
    inputs = [("ca", path) for path in ca_paths]
    if cert is not None:
        inputs.append(("cert", cert))
    if key is not None:
        inputs.append(("key", key))
    if not inputs:
        return ca_paths, cert, key
    if len(inputs) > _MAX_TLS_FILES:
        return None

    contents: list[tuple[str, bytes]] = []
    total = 0
    for label, path in inputs:
        data = _read_tls_file(path, deadline)
        if data is None or time.monotonic() >= deadline:
            return None
        total += len(data)
        if total > _MAX_TLS_TOTAL_BYTES:
            return None
        contents.append((label, data))

    directory = Path(
        resources.enter_context(
            tempfile.TemporaryDirectory(prefix="protodial-completion-tls-")
        )
    )
    snapshots: list[tuple[str, str]] = []
    for index, (label, data) in enumerate(contents):
        if time.monotonic() >= deadline:
            return None
        snapshot = directory / f"{index}-{label}.pem"
        snapshot.write_bytes(data)
        snapshots.append((label, str(snapshot)))
    saved_ca = tuple(path for label, path in snapshots if label == "ca")
    saved_cert = next(
        (path for label, path in snapshots if label == "cert"), None
    )
    saved_key = next(
        (path for label, path in snapshots if label == "key"), None
    )
    return saved_ca, saved_cert, saved_key


def reflection_source(
    scan: Scan, environment: Mapping[str, str]
) -> OpenedReflectionSource | None:
    endpoint = reflection_endpoint(scan)
    if endpoint is None:
        return None
    deadline = time.monotonic() + _completion_timeout(environment)
    resources: ExitStack | None = None
    try:
        from ..argument_parser.resolution import resolve_transport_mode
        from ..argument_parser.target import parse_target
        from ..cli import load_runtime
        from ..commands.models import (
            Command,
            ConnectionOptions,
            DiagnosticOptions,
            MetadataOptions,
            SchemaOptions,
            TlsOptions,
        )
        from ..commands.operations import ListSymbols
        from ..request_metadata.parsing import parse_metadata_scopes
        from ..schema.factory import build_descriptor_source
        from ..transport import ready_channel

        target = parse_target(endpoint)
        plaintext = "--plaintext" in scan.flags
        insecure = "--insecure" in scan.flags or "-k" in scan.flags
        cert = scan.last("--cert")
        key = scan.last("--key")
        key_password = scan.last("--key-password")
        server_name = scan.last("--servername")
        authority = scan.last("--authority")
        if (
            plaintext
            and insecure
            or bool(cert) != bool(key)
            or key_password is not None
            and not key
            or server_name is not None
            and not server_name.strip()
            or authority is not None
            and not authority.strip()
        ):
            return None
        resources = ExitStack()
        snapshots = _snapshot_tls_files(
            tuple(scan.values.get("--cacert", ())),
            cert,
            key,
            deadline,
            resources,
        )
        if snapshots is None:
            resources.close()
            return None
        ca_paths, cert, key = snapshots

        class _ValidationParser(argparse.ArgumentParser):
            def error(self, message: str) -> NoReturn:
                raise ValueError(message)

        namespace = argparse.Namespace(
            plaintext=plaintext,
            insecure=insecure,
            cacert=ca_paths,
            cert=cert,
            key=key,
            key_password=key_password,
            servername=server_name,
        )
        mode = resolve_transport_mode(
            _ValidationParser(add_help=False), namespace, target
        )
        command = Command(
            target=target,
            operation=ListSymbols(None),
            schema=SchemaOptions((), (), (), use_reflection=False),
            transport=TlsOptions(
                mode=mode,
                ca_paths=ca_paths,
                cert=cert,
                key=key,
                key_password=key_password,
                server_name=server_name,
                authority=authority,
            ),
            connection=ConnectionOptions(
                connect_timeout=min(
                    _bounded_timeout(scan, "--connect-timeout", environment),
                    max(0.001, deadline - time.monotonic()),
                ),
                max_recv_message_size=_integer(scan, "--max-recv-message-size"),
                max_send_message_size=_integer(scan, "--max-send-message-size"),
                compression=scan.last("--compression") or "none",
                keepalive_time=(
                    _number(scan, "--keepalive-time", 0.0)
                    if scan.last("--keepalive-time") is not None
                    else None
                ),
                user_agent=scan.last("--user-agent", "-A"),
            ),
            metadata=MetadataOptions(
                tuple(scan.values.get("--header", ())),
                tuple(scan.values.get("--reflect-header", ())),
                (),
            ),
            diagnostics=DiagnosticOptions(False, False, False),
            timeout=min(
                _bounded_timeout(scan, "--max-time", environment),
                max(0.001, deadline - time.monotonic()),
            ),
            invoke_options=None,
            get_template=False,
            template_depth=8,
            show_reflection=False,
        )
        runtime = load_runtime()
        metadata = parse_metadata_scopes(
            command.metadata.header, command.metadata.reflect_header
        )
        # The reflection RPC below already has the shared completion deadline.
        # Waiting for channel readiness first adds a second network wait to
        # every Tab press and makes an unavailable endpoint feel sluggish.
        channel_context = cast(
            AbstractContextManager[object],
            cast(
                object,
                ready_channel(command, runtime.grpc, await_ready=False),
            ),
        )
        channel = resources.enter_context(channel_context)
        source = build_descriptor_source(
            command,
            runtime.grpc,
            cast(ReflectionChannel, channel),
            metadata.reflection,
            command.transport.authority or command.target.authority or "",
            reflection_deadline=deadline,
        )
        return OpenedReflectionSource(source, resources, deadline)
    except Exception:
        if resources is not None:
            try:
                resources.close()
            except Exception:
                pass
        return None


class OpenedReflectionSource:
    """Keep the reflection channel alive while candidates are collected."""

    def __init__(
        self,
        source: DescriptorSource,
        context: AbstractContextManager[object],
        deadline: float | None = None,
    ) -> None:
        self.source = source
        self._context = context
        if deadline is not None:
            setattr(source, "_protodial_completion_deadline", deadline)

    def close(self) -> None:
        exit_method = getattr(self._context, "__exit__", None)
        if exit_method is not None:
            try:
                exit_method(None, None, None)
            except Exception:
                pass
