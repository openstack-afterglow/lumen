"""DNS-pinned HTTP boundary for chat-owned outbound requests.

The legacy ``validate_url`` helper remains for configuration-time checks. Runtime
requests must use :class:`SafeAsyncTransport`: it resolves the original hostname,
rejects every non-public answer, then opens the TCP socket to that validated IP
while preserving the hostname for HTTP Host and TLS SNI. This closes the
validate-then-re-resolve DNS-rebinding gap.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpx._transports.default import AsyncResponseStream, map_httpcore_exceptions

_BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}
_DEFAULT_PORTS = {"http": 80, "https": 443}
_Resolver = Callable[[str, int], Awaitable[list[str]]]


class SsrfBlocked(ValueError):
    """Outbound request violates the SSRF policy."""


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not ip.is_global or ip.is_multicast


def _normalize_hostname(value: str) -> str:
    hostname = value.strip().rstrip(".")
    if not hostname or any(char.isspace() or ord(char) < 32 for char in hostname):
        raise SsrfBlocked("hostname is required")
    try:
        return hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise SsrfBlocked("hostname is invalid") from exc


def _matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _validate_domain_policy(hostname: str, allowed_domains: Iterable[str], blocked_domains: Iterable[str]) -> None:
    allowed = tuple(_normalize_hostname(domain) for domain in allowed_domains)
    blocked = tuple(_normalize_hostname(domain) for domain in blocked_domains)
    if any(_matches_domain(hostname, domain) for domain in blocked):
        raise SsrfBlocked("blocked domain")
    if allowed and not any(_matches_domain(hostname, domain) for domain in allowed):
        raise SsrfBlocked("domain is not allowed")


def _parse_and_validate_url(
    url: str,
    *,
    allowed_domains: Iterable[str] = (),
    blocked_domains: Iterable[str] = (),
) -> tuple[str, int, str]:
    parsed = urlsplit(url)
    if parsed.scheme not in _DEFAULT_PORTS:
        raise SsrfBlocked("only http/https URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise SsrfBlocked("URL credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise SsrfBlocked("URL port is invalid") from exc
    if port is not None and port not in _DEFAULT_PORTS.values():
        raise SsrfBlocked("only ports 80 and 443 are allowed")
    if parsed.scheme == "https" and port not in (None, 443):
        raise SsrfBlocked("HTTPS requires port 443")
    if parsed.scheme == "http" and port not in (None, 80):
        raise SsrfBlocked("HTTP requires port 80")
    if not parsed.hostname:
        raise SsrfBlocked("hostname is required")

    hostname = _normalize_hostname(parsed.hostname)
    if hostname in _BLOCKED_HOSTNAMES:
        raise SsrfBlocked(f"blocked hostname: {hostname}")
    _validate_domain_policy(hostname, allowed_domains, blocked_domains)

    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None and _is_blocked_ip(ip):
        raise SsrfBlocked("private or internal IP addresses are not allowed")
    return hostname, port or _DEFAULT_PORTS[parsed.scheme], parsed.scheme


def _resolve_public_addresses_sync(hostname: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfBlocked("hostname cannot be resolved") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise SsrfBlocked("hostname cannot be resolved")
    for address in addresses:
        try:
            resolved = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SsrfBlocked("hostname resolved to an invalid address") from exc
        if _is_blocked_ip(resolved):
            raise SsrfBlocked("hostname resolves to a private or internal IP address")
    return addresses


async def resolve_public_addresses(hostname: str, port: int) -> list[str]:
    """Resolve and validate every address before a pinned socket is opened."""
    return await asyncio.to_thread(_resolve_public_addresses_sync, hostname, port)


def validate_url(url: str) -> str:
    """Configuration-time URL validation, including DNS resolution.

    Runtime callers must use ``SafeAsyncTransport`` instead so their actual
    connection remains pinned to these validated public addresses.
    """
    hostname, port, _ = _parse_and_validate_url(url)
    _resolve_public_addresses_sync(hostname, port)
    return url


class _DnsPinningNetworkBackend(httpcore.AsyncNetworkBackend):
    """Open TCP only to a public address resolved for the requested hostname."""

    def __init__(self, resolver: _Resolver) -> None:
        self._resolver = resolver
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[tuple[int, int, int]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        deadline = asyncio.get_running_loop().time() + timeout if timeout is not None else None
        if deadline is None:
            addresses = await self._resolver(host, port)
        else:
            async with asyncio.timeout(timeout):
                addresses = await self._resolver(host, port)
        if not addresses:
            raise SsrfBlocked("hostname cannot be resolved")
        for address in addresses:
            try:
                resolved = ipaddress.ip_address(address)
            except ValueError as exc:
                raise SsrfBlocked("hostname resolved to an invalid address") from exc
            if _is_blocked_ip(resolved):
                raise SsrfBlocked("hostname resolves to a private or internal IP address")
        last_error: Exception | None = None
        for address in addresses:
            remaining = None if deadline is None else deadline - asyncio.get_running_loop().time()
            if remaining is not None and remaining <= 0:
                raise TimeoutError("DNS-pinned connection timed out")
            try:
                return await self._backend.connect_tcp(
                    host=address,
                    port=port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # Try each validated A/AAAA answer.
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[tuple[int, int, int]] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SsrfBlocked("Unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


def _validate_request_authority(request: httpx.Request, hostname: str, port: int, scheme: str) -> None:
    host_header = request.headers.get("host")
    if not host_header:
        raise SsrfBlocked("Host header is required")
    authority = urlsplit(f"//{host_header}")
    if authority.username is not None or authority.password is not None or not authority.hostname:
        raise SsrfBlocked("Host header is invalid")
    try:
        authority_port = authority.port
    except ValueError as exc:
        raise SsrfBlocked("Host header port is invalid") from exc
    if _normalize_hostname(authority.hostname) != hostname:
        raise SsrfBlocked("Host header does not match URL authority")
    if authority_port not in (None, port, _DEFAULT_PORTS[scheme]):
        raise SsrfBlocked("Host header port does not match URL authority")


class _BoundedResponseStream(httpx.AsyncByteStream):
    def __init__(self, stream: httpx.AsyncByteStream, maximum_bytes: int) -> None:
        self._stream = stream
        self._maximum_bytes = maximum_bytes

    async def __aiter__(self):
        total = 0
        async for chunk in self._stream:
            total += len(chunk)
            if total > self._maximum_bytes:
                raise SsrfBlocked("response exceeds the configured byte limit")
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


class SafeAsyncTransport(httpx.AsyncBaseTransport):
    """An HTTPX transport with strict URL policy and DNS-pinned connections.

    Redirects are deliberately disabled at the client boundary. A caller that
    needs redirects must inspect each ``Location`` and issue a fresh request;
    every hop is therefore validated by this transport.
    """

    def __init__(
        self,
        *,
        allowed_domains: Iterable[str] = (),
        blocked_domains: Iterable[str] = (),
        max_response_bytes: int | None = None,
        resolver: _Resolver = resolve_public_addresses,
    ) -> None:
        if max_response_bytes is not None and max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._allowed_domains = tuple(_normalize_hostname(domain) for domain in allowed_domains)
        self._blocked_domains = tuple(_normalize_hostname(domain) for domain in blocked_domains)
        self._max_response_bytes = max_response_bytes
        self._pool = httpcore.AsyncConnectionPool(network_backend=_DnsPinningNetworkBackend(resolver), http2=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        hostname, _port, _scheme = _parse_and_validate_url(
            str(request.url),
            allowed_domains=self._allowed_domains,
            blocked_domains=self._blocked_domains,
        )
        _validate_request_authority(request, hostname, _port, _scheme)
        # httpcore retains the original request host for Host/TLS SNI; only its
        # network backend substitutes the validated IP at socket-open time.
        request.headers["accept-encoding"] = "identity"
        headers = request.headers.raw
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=headers,
            content=request.stream,
            extensions=request.extensions,
        )
        with map_httpcore_exceptions():
            response = await self._pool.handle_async_request(core_request)
        content_encoding = httpx.Headers(response.headers).get("content-encoding", "identity").strip().lower()
        if content_encoding not in ("", "identity"):
            await response.stream.aclose()
            raise SsrfBlocked("compressed responses are not allowed")
        stream: httpx.AsyncByteStream = AsyncResponseStream(response.stream)
        if self._max_response_bytes is not None:
            stream = _BoundedResponseStream(stream, self._max_response_bytes)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=stream,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()
