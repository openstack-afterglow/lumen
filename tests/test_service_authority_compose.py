"""Isolated system-test directory boundary rejects unintended outbound requests."""

import httpx
import pytest


@pytest.mark.parametrize(("url", "system", "allowed"), [
    ("http://fake-keystone:5000/health", True, True),
    ("http://fake-keystone:5000/health", False, False),
    ("https://fake-keystone:5000/health", True, False),
    ("http://fake-keystone:5001/health", True, False),
])
def test_test_only_directory_allowlist_requires_exact_isolated_stack_boundary(monkeypatch, url, system, allowed):
    monkeypatch.setenv("LUMEN_API_BASE_URL", "http://lumen-api:8012" if system else "http://localhost:8012")
    calls = []
    def send(self, request):
        calls.append(request)
        return httpx.Response(200, content=b"synthetic directory")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", send)
    with httpx.Client(trust_env=False) as client:
        if allowed:
            assert client.get(url).status_code == 200
            assert len(calls) == 1
        else:
            with pytest.raises(AssertionError, match="unmocked outbound HTTP"):
                client.get(url)
            assert calls == []
