"""OpenStack SDK proxy for Lumen v1."""

from __future__ import annotations

from openstack import proxy

from ._api import _LumenApiMixin


class Proxy(_LumenApiMixin, proxy.Proxy):
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
        kwargs: dict = {}
        if body:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        if files:
            kwargs["files"] = files
        if headers:
            kwargs["headers"] = headers
        response = self.request(path, method, raise_exc=True, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def _text_request(self, method: str, path: str, *, params: dict | None = None) -> str:
        kwargs = {"params": params} if params else {}
        response = self.request(path, method, raise_exc=True, **kwargs)
        return response.text

    def _stream_request(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ):
        """Issue a streaming (SSE) request and return a line iterator.

        ``stream=True`` is forwarded through the SDK session so the response
        body is not buffered — each yielded item is one decoded line of the
        upstream ``text/event-stream``.
        """
        kwargs: dict = {"stream": True}
        if body:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        if headers:
            kwargs["headers"] = headers
        response = self.request(path, method, raise_exc=True, **kwargs)
        return response.iter_lines(decode_unicode=True)

    # -- Conversations & Messages ---------------------------------------
