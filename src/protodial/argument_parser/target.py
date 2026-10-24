"""Parsing for gRPC authorities, resolver targets, and invocation URLs."""

from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit, urlunsplit

from ..commands.models import ParsedTarget
from ..errors import CliError

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def parse_target(target: str) -> ParsedTarget:
    """Parse a curl-style URL or preserve an opaque grpcio target."""
    if (
        not target
        or target != target.strip()
        or any(character.isspace() for character in target)
    ):
        raise CliError("target must be a non-empty authority or resolver URI")

    scheme_match = _SCHEME.match(target)
    if scheme_match is not None:
        scheme = target[: scheme_match.end() - 1].lower()
        if scheme in {"grpc", "grpcs"}:
            return _parse_invocation_url(_split_target(target))
        # A conventional HOST:PORT is syntactically indistinguishable from an
        # opaque URI. Prefer the direct-address interpretation for a decimal
        # port so reflection and the insecure TLS bridge retain typed endpoint
        # data, while preserving every other scheme as an opaque grpcio target.
        if target[scheme_match.end() :].isdecimal():
            host, port = _validate_bare_target(target)
            return ParsedTarget(
                target,
                authority=target,
                tunnel_host=host,
                tunnel_port=port,
            )
        return ParsedTarget(
            target, authority=_resolver_authority(target, scheme)
        )

    host, port = _validate_bare_target(target)
    return ParsedTarget(
        target,
        authority=target,
        tunnel_host=host,
        tunnel_port=port,
    )


def parse_discovery_target(
    target: str,
) -> tuple[ParsedTarget, str | None]:
    """Parse a discovery target and an optional symbol or method URL path."""
    scheme_match = _SCHEME.match(target)
    if scheme_match is None or target[: scheme_match.end() - 1].lower() not in {
        "grpc",
        "grpcs",
    }:
        return parse_target(target), None

    parsed = _split_target(target)
    if not parsed.path:
        return parse_target(target), None
    if parsed.query or parsed.fragment:
        # Reuse the public parser's stable URL diagnostics.
        return parse_target(target), None

    path_parts = parsed.path.split("/")
    if (
        len(path_parts) not in {2, 3}
        or path_parts[0]
        or not all(path_parts[1:])
    ):
        raise CliError(
            "discovery URL path must have the form /SYMBOL or /SERVICE/METHOD"
        )
    endpoint = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return parse_target(endpoint), "/".join(path_parts[1:])


def _split_target(target: str) -> SplitResult:
    try:
        return urlsplit(target)
    except ValueError as error:
        raise CliError(f"invalid target URL: {error}") from None


def _parse_invocation_url(parsed: SplitResult) -> ParsedTarget:
    try:
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as error:
        raise CliError(f"invalid target URL: {error}") from None

    if (
        not parsed.netloc
        or hostname is None
        or parsed.query
        or parsed.fragment
        or username is not None
        or password is not None
    ):
        raise CliError(
            "target URL must have an authority and no credentials, query, or "
            "fragment"
        )
    if _has_empty_port(parsed.netloc):
        raise CliError("target URL port must not be empty")
    if port == 0:
        raise CliError("target URL port must be positive")

    if not parsed.path:
        method = None
    else:
        path_parts = parsed.path.split("/")
        if len(path_parts) != 3 or path_parts[0] or not all(path_parts[1:]):
            raise CliError(
                "target URL method must have the form /SERVICE/METHOD"
            )
        method = "/".join(path_parts[1:])

    return ParsedTarget(
        parsed.netloc,
        method=method,
        transport=("plaintext" if parsed.scheme.lower() == "grpc" else "tls"),
        authority=parsed.netloc,
        tunnel_host=hostname,
        tunnel_port=port,
    )


def _resolver_authority(target: str, scheme: str) -> str | None:
    """Derive a reflection host only when a resolver names one endpoint."""
    remainder = target[len(scheme) + 1 :]
    if not remainder or not remainder.lstrip("/"):
        raise CliError("grpcio target URI must include a resolver endpoint")
    if scheme not in {"dns", "dns+srv", "ipv4", "ipv6"}:
        return None

    if remainder.startswith("//"):
        _resolver, separator, endpoint = remainder[2:].partition("/")
        if not separator or not endpoint:
            return None
    else:
        endpoint = remainder.lstrip("/")
    if not endpoint or "," in endpoint or "/" in endpoint:
        return None
    return endpoint


def _has_empty_port(authority: str) -> bool:
    if authority.startswith("["):
        closing = authority.find("]")
        return closing >= 0 and authority[closing + 1 :] == ":"
    return authority.endswith(":")


def _validate_unbracketed_ipv6_target(target: str) -> None:
    """Validate a bare IPv6 address with an optional unambiguous port."""
    try:
        ip_address(target)
    except ValueError:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise CliError("invalid unbracketed IPv6 target")
        try:
            address = ip_address(host)
            port = int(port_text)
        except ValueError as error:
            raise CliError("invalid unbracketed IPv6 target") from error
        if address.version != 6 or not 1 <= port <= 65_535:
            raise CliError("invalid unbracketed IPv6 target")


def validate_insecure_target(target: ParsedTarget) -> None:
    """Require the TCP authority supported by the insecure TLS bridge."""
    if target.tunnel_host is None or target.tunnel_port is None:
        raise CliError(
            "--insecure requires a TCP target of HOST:PORT or [IPv6]:PORT"
        )


def _validate_bare_target(target: str) -> tuple[str, int | None]:
    try:
        address = ip_address(target)
    except ValueError:
        pass
    else:
        return str(address), None

    # gRPC accepts unbracketed IPv6 channel targets, which are not URL
    # authorities.
    if target.count(":") > 1 and not any(
        character in target for character in "/[]"
    ):
        _validate_unbracketed_ipv6_target(target)
        host, separator, port_text = target.rpartition(":")
        return host, int(port_text) if separator else None

    try:
        parsed = urlsplit(f"//{target}")
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise CliError(f"invalid target authority: {error}") from None

    if parsed.path or parsed.query or parsed.fragment:
        raise CliError(
            "bare targets cannot contain a path; use a grpc:// or grpcs:// URL"
        )
    if (
        hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise CliError("bare target must be an authority without credentials")
    if _has_empty_port(target):
        raise CliError("target port must not be empty")
    if port == 0:
        raise CliError("target port must be positive")
    return hostname, port
