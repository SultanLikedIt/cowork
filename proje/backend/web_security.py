"""Public-network URL validation for the browser-free web engine."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

MAX_URL_CHARS = 4_096
ALLOWED_SCHEMES = frozenset({"http", "https"})


class UnsafeTarget(ValueError):
    """A request target is not a public HTTP(S) destination."""


@dataclass(frozen=True)
class ValidatedTarget:
    url: str
    hostname: str
    port: int


Resolver = Callable[..., list]


def validate_public_url(
    url: str, resolver: Resolver = socket.getaddrinfo
) -> ValidatedTarget:
    """Resolve and reject every non-public request/redirect target."""
    value = str(url or "").strip()
    if not value or len(value) > MAX_URL_CHARS:
        raise UnsafeTarget("URL is empty or exceeds the bounded URL length.")

    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeTarget("Only public http and https URLs are allowed.")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeTarget("Credentials embedded in URLs are not allowed.")
    host = parsed.hostname
    if not host:
        raise UnsafeTarget("URL has no hostname.")
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeTarget("URL contains an invalid port.") from exc

    try:
        addresses = resolver(host, port, 0, socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise UnsafeTarget(f"Could not resolve web target {host!r}.") from exc
    if not addresses:
        raise UnsafeTarget(f"Could not resolve web target {host!r}.")

    for address_info in addresses:
        address = address_info[4][0]
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeTarget(f"Web target {host!r} resolved unexpectedly.") from exc
        if (
            not resolved.is_global
            or resolved.is_loopback
            or resolved.is_link_local
            or resolved.is_private
            or resolved.is_reserved
            or resolved.is_multicast
            or resolved.is_unspecified
        ):
            raise UnsafeTarget(
                f"Refusing web target {host!r}: it resolves to a non-public address."
            )

    return ValidatedTarget(url=value, hostname=host.lower(), port=port)


__all__ = [
    "ALLOWED_SCHEMES",
    "UnsafeTarget",
    "ValidatedTarget",
    "validate_public_url",
]
