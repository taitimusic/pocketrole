"""URL validation helpers for externally configured HTTP endpoints."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit


class PublicHttpUrlValidationError(ValueError):
    """Raised when a public HTTP(S) endpoint URL is unsafe or unsupported."""


class FeedUrlValidationError(PublicHttpUrlValidationError):
    """Raised when a news feed URL is unsafe or unsupported."""


def _restricted_ip(value: str) -> bool:
    ip = ip_address(value)
    if getattr(ip, "ipv4_mapped", None) is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_public_http_url(
    url: str,
    *,
    allow_private_hosts: bool = False,
    error_cls: type[PublicHttpUrlValidationError] = PublicHttpUrlValidationError,
    private_host_label: str = "private hosts are disabled",
    localhost_label: str = "localhost hosts are disabled",
) -> str:
    candidate = str(url or "").strip()
    if not candidate:
        raise error_cls("url is required")
    if any(ch.isspace() for ch in candidate):
        raise error_cls("url must not contain whitespace")

    parts = urlsplit(candidate)
    if parts.scheme.lower() not in {"http", "https"}:
        raise error_cls("url must use http or https")
    if parts.username is not None or parts.password is not None:
        raise error_cls("url must not contain userinfo")
    if not parts.hostname:
        raise error_cls("url host is required")
    try:
        _ = parts.port
    except ValueError as exc:
        raise error_cls("url port is invalid") from exc

    host = parts.hostname.rstrip(".").lower()
    if "%" in host:
        raise error_cls("url host must not contain a zone identifier")
    if host == "localhost" or host.endswith(".localhost"):
        if not allow_private_hosts:
            raise error_cls(localhost_label)
        return candidate

    try:
        host_is_restricted_ip = _restricted_ip(host)
    except ValueError:
        try:
            host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise error_cls("url host is invalid") from exc
    else:
        if host_is_restricted_ip and not allow_private_hosts:
            raise error_cls(private_host_label)

    return candidate


def validate_public_http_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate a public HTTP(S) endpoint and return its stripped form."""
    return _validate_public_http_url(
        url,
        allow_private_hosts=allow_private_hosts,
        error_cls=PublicHttpUrlValidationError,
    )


def validate_feed_url(url: str, *, allow_private_hosts: bool = False) -> str:
    """Validate a feed/article URL and return its stripped form.

    The default policy accepts only public HTTP(S) endpoints without userinfo.
    """
    return _validate_public_http_url(
        url,
        allow_private_hosts=allow_private_hosts,
        error_cls=FeedUrlValidationError,
        private_host_label="private feed hosts are disabled",
        localhost_label="localhost feed hosts are disabled",
    )


def is_public_http_url(url: str, *, allow_private_hosts: bool = False) -> bool:
    try:
        validate_public_http_url(url, allow_private_hosts=allow_private_hosts)
    except PublicHttpUrlValidationError:
        return False
    return True
