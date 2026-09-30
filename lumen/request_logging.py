"""ASGI request outcomes with route templates rather than request targets."""

from __future__ import annotations

import logging

from starlette.routing import Route

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        if (
            not isinstance(method, str)
            or not 0 < len(method) <= 32
            or not all("A" <= char <= "Z" or char == "-" for char in method)
        ):
            method = "<invalid>"
        status = None
        complete = False
        outcome = "failed"

        async def observe(message):
            nonlocal status, complete
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, receive, observe)
            outcome = status if complete else "incomplete"
        except Exception:
            outcome = 500 if status is None else "incomplete"
            raise
        finally:
            route = scope.get("route")
            template = route.path_format if isinstance(route, Route) else "<unmatched>"
            logger.info("http request method=%s route=%s status=%s", method, template, outcome)
