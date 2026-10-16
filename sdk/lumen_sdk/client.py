"""API-key clients for the direct Lumen `/v1` surface."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx

from ._api import _LumenApiMixin, _segment


class Client(_LumenApiMixin):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
        transport: httpx.BaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def request(self, path: str, method: str, *, raise_exc: bool = True, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("allow_redirects", None)
        kwargs.pop("stream", None)
        response = self._client.request(method, self._url(path), follow_redirects=follow_redirects, **kwargs)
        if raise_exc:
            response.raise_for_status()
        return response

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ):
        response = self.request(path, method, json=body, params=params, files=files, headers=headers)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _text_request(self, method: str, path: str, *, params: dict | None = None) -> str:
        return self.request(path, method, params=params).text

    def _stream_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Iterator[str]:
        def lines() -> Iterator[str]:
            with self._client.stream(method, self._url(path), json=body, params=params, headers=headers) as response:
                response.raise_for_status()
                yield from response.iter_lines()

        return lines()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class AsyncClient(_LumenApiMixin):
    """Asynchronous client: every shared route method returns an awaitable."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            verify=verify,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def request(self, path: str, method: str, *, raise_exc: bool = True, **kwargs: Any) -> httpx.Response:
        follow_redirects = kwargs.pop("allow_redirects", None)
        kwargs.pop("stream", None)
        response = await self._client.request(method, self._url(path), follow_redirects=follow_redirects, **kwargs)
        if raise_exc:
            response.raise_for_status()
        return response

    async def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ):
        response = await self.request(path, method, json=body, params=params, files=files, headers=headers)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _text_request(self, method: str, path: str, *, params: dict | None = None) -> str:
        return (await self.request(path, method, params=params)).text

    def _stream_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> AsyncIterator[str]:
        async def lines() -> AsyncIterator[str]:
            async with self._client.stream(method, self._url(path), json=body, params=params, headers=headers) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    yield line

        return lines()

    # Byte-returning shared routes read the response body after the awaited request.
    async def speech(self, *, idempotency_key: str, **attrs) -> bytes:
        response = await self.request("/v1/chat/audio/speech", "POST", raise_exc=True, json=attrs,
                                      headers={"Idempotency-Key": idempotency_key})
        return response.content

    async def download_asset(self, asset_id) -> bytes:
        response = await self.request(f"/v1/assets/{_segment(asset_id)}/download", "GET", raise_exc=True)
        return response.content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()
