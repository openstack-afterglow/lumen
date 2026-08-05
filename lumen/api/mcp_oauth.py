"""Public callback endpoint for user-initiated remote MCP OAuth."""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Cookie, Query
from fastapi.responses import HTMLResponse, RedirectResponse

from lumen.config import get_settings
from lumen.services import mcp_oauth

router = APIRouter()
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _clear_initiator_cookie(response):
    response.delete_cookie(mcp_oauth.INITIATOR_COOKIE, path="/api/v1/chat/mcp-oauth/callback")
    return response


def _return_url(*, connected: bool, server_id: int | None = None, return_origin: str | None = None) -> str | None:
    raw = mcp_oauth.approved_return_origin(return_origin) or get_settings().frontend_base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if (
        (
            parsed.scheme != "https"
            and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
        )
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    query = {"mcp_oauth": "connected" if connected else "failed"}
    if server_id is not None:
        query["mcp_server_id"] = str(server_id)
    return urlunsplit((parsed.scheme, parsed.netloc, f"{parsed.path.rstrip('/')}/dashboard/chat", urlencode(query), ""))


@router.get("/mcp-oauth/callback")
async def callback(
    state: str = Query(min_length=1, max_length=512),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=128),
    iss: str | None = Query(default=None, max_length=2048),
    initiator_nonce: str | None = Cookie(default=None, alias=mcp_oauth.INITIATOR_COOKIE),
):
    try:
        server_id, return_origin = await mcp_oauth.complete(
            state=state, code=code, error=error, iss=iss, initiator_nonce=initiator_nonce
        )
    except mcp_oauth.McpOAuthError as exc:
        target = _return_url(connected=False, return_origin=getattr(exc, "return_origin", None))
        if target:
            return _clear_initiator_cookie(RedirectResponse(target, status_code=303, headers=_HEADERS))
        return _clear_initiator_cookie(
            HTMLResponse(
                "<h1>OAuth connection failed</h1><p>Return to Afterglow and reconnect the MCP server.</p>",
                status_code=400,
                headers=_HEADERS,
            )
        )
    target = _return_url(connected=True, server_id=server_id, return_origin=return_origin)
    if target:
        return _clear_initiator_cookie(RedirectResponse(target, status_code=303, headers=_HEADERS))
    return _clear_initiator_cookie(
        HTMLResponse("<h1>OAuth connection completed</h1><p>You can return to Afterglow.</p>", headers=_HEADERS)
    )
