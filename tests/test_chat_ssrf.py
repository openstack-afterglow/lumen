"""커스텀 툴 SSRF 가드 테스트 — 내부/사설/메타데이터 차단, 공용 허용.

crypto 처럼 외부 인프라 불요(단, DNS 해석은 monkeypatch).
"""

import asyncio
import socket

import httpx
import pytest

from lumen.services import ssrf
from lumen.services.ssrf import SsrfBlocked, validate_url


def _gai(ip: str):
    def _fn(*a, **k):
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 443))]

    return _fn


class TestScheme:
    @pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://x"])
    def test_blocks_non_http(self, url):
        with pytest.raises(SsrfBlocked):
            validate_url(url)


class TestIpLiterals:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://10.0.0.5/",
            "http://192.168.1.1/",
            "http://172.16.0.1/",
            "http://169.254.169.254/latest/meta-data/",  # 클라우드 메타데이터
            "http://[::1]/",
            "http://0.0.0.0/",
            "http://100.64.0.1/",  # CGNAT
            "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
            "http://224.0.0.1/",  # multicast is not a public destination
        ],
    )
    def test_blocks_internal_ip(self, url):
        with pytest.raises(SsrfBlocked):
            validate_url(url)

    def test_allows_public_ip(self):
        assert validate_url("http://93.184.216.34/") == "http://93.184.216.34/"


class TestHostnames:
    def test_blocks_localhost(self):
        with pytest.raises(SsrfBlocked):
            validate_url("http://localhost/")

    def test_blocks_metadata_hostname(self):
        with pytest.raises(SsrfBlocked):
            validate_url("http://metadata.google.internal/")

    def test_blocks_host_resolving_to_private(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _gai("10.1.2.3"))
        with pytest.raises(SsrfBlocked):
            validate_url("http://evil.example.com/")

    def test_blocks_host_resolving_to_metadata(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _gai("169.254.169.254"))
        with pytest.raises(SsrfBlocked):
            validate_url("http://rebind.example.com/")

    def test_allows_host_resolving_public(self, monkeypatch):
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _gai("93.184.216.34"))
        assert validate_url("http://good.example.com/") == "http://good.example.com/"

    def test_blocks_unresolvable(self, monkeypatch):
        def _raise(*a, **k):
            raise socket.gaierror("no such host")

        monkeypatch.setattr(ssrf.socket, "getaddrinfo", _raise)
        with pytest.raises(SsrfBlocked):
            validate_url("http://nonexistent.invalid/")


class TestPinnedTransport:
    async def test_rejects_mismatched_host_header_before_connect(self):
        async def resolver(host: str, port: int) -> list[str]:
            raise AssertionError(f"resolver must not run for mismatched authority: {host}:{port}")

        async with httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(resolver=resolver),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with pytest.raises(SsrfBlocked, match="Host header"):
                await client.get("https://example.com/", headers={"Host": "internal.example"})

    async def test_rejects_private_address_from_custom_resolver(self):
        async def resolver(host: str, port: int) -> list[str]:
            assert (host, port) == ("example.com", 443)
            return ["127.0.0.1"]

        async with httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(resolver=resolver),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with pytest.raises(SsrfBlocked, match="private"):
                await client.get("https://example.com/")

    async def test_enforces_domain_policy_before_dns_lookup(self):
        async def resolver(host: str, port: int) -> list[str]:
            raise AssertionError(f"resolver must not run for blocked domain: {host}:{port}")

        async with httpx.AsyncClient(
            transport=ssrf.SafeAsyncTransport(allowed_domains=["docs.example"], resolver=resolver),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with pytest.raises(SsrfBlocked, match="not allowed"):
                await client.get("https://api.example/")

    async def test_dns_resolution_obeys_connect_timeout(self):
        async def stalled_resolver(host: str, port: int) -> list[str]:
            await asyncio.sleep(0.05)
            return ["93.184.216.34"]

        backend = ssrf._DnsPinningNetworkBackend(stalled_resolver)
        with pytest.raises(TimeoutError):
            await backend.connect_tcp("example.com", 443, timeout=0.001)

    async def test_forces_identity_encoding_on_the_core_request(self):
        captured: dict = {}

        class _Stream:
            async def __aiter__(self):
                if False:
                    yield b""

            async def aclose(self):
                return None

        class _Pool:
            async def handle_async_request(self, request):
                captured["headers"] = request.headers
                return type("Response", (), {"status": 200, "headers": [], "stream": _Stream(), "extensions": {}})()

            async def aclose(self):
                return None

        async def resolver(host: str, port: int) -> list[str]:
            return ["93.184.216.34"]

        transport = ssrf.SafeAsyncTransport(resolver=resolver)
        transport._pool = _Pool()
        request = httpx.Request("GET", "https://example.com/", headers={"Accept-Encoding": "gzip"})
        response = await transport.handle_async_request(request)
        try:
            assert (b"accept-encoding", b"identity") in captured["headers"]
            assert request.headers["accept-encoding"] == "identity"
        finally:
            await response.aclose()

    async def test_rejects_compressed_response_before_httpx_decodes_it(self):
        closed = False

        class _Stream:
            async def __aiter__(self):
                raise AssertionError("compressed stream must not be read")
                yield b""  # pragma: no cover

            async def aclose(self):
                nonlocal closed
                closed = True

        class _Pool:
            async def handle_async_request(self, request):
                return type(
                    "Response",
                    (),
                    {"status": 200, "headers": [(b"content-encoding", b"gzip")], "stream": _Stream(), "extensions": {}},
                )()

            async def aclose(self):
                return None

        async def resolver(host: str, port: int) -> list[str]:
            return ["93.184.216.34"]

        transport = ssrf.SafeAsyncTransport(resolver=resolver)
        transport._pool = _Pool()
        with pytest.raises(SsrfBlocked, match="compressed"):
            await transport.handle_async_request(httpx.Request("GET", "https://example.com/"))
        assert closed is True
