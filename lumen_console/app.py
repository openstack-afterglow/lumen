"""Local browser console with independent users and an in-memory Lumen connection vault."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .store import LocalUser, LocalUserStore

_COOKIE_NAME = "lumen_console_session"
_STATIC_DIR = Path(__file__).with_name("static")


@dataclass(frozen=True)
class LumenConnection:
    base_url: str
    auth_mode: str
    credential: str
    project_id: str | None

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_mode == "api_key":
            headers["Authorization"] = f"Bearer {self.credential}"
        else:
            headers["X-Auth-Token"] = self.credential
            if self.project_id:
                headers["X-Project-Id"] = self.project_id
        return headers


class ConnectionVault:
    """Keeps upstream credentials only in process memory, scoped to a local session."""

    def __init__(self) -> None:
        self._connections: dict[str, LumenConnection] = {}

    def get(self, session_token: str) -> LumenConnection | None:
        return self._connections.get(session_token)

    def set(self, session_token: str, connection: LumenConnection) -> None:
        self._connections[session_token] = connection

    def discard(self, session_token: str | None) -> None:
        if session_token:
            self._connections.pop(session_token, None)


class CredentialsBody(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=10, max_length=256)


class ConnectionBody(BaseModel):
    base_url: str = Field(min_length=1, max_length=512)
    auth_mode: str = Field(pattern="^(api_key|keystone)$")
    credential: str = Field(min_length=1, max_length=4_096)
    project_id: str | None = Field(default=None, max_length=64)


class ChatBody(BaseModel):
    model_id: str = Field(min_length=1, max_length=190)
    text: str = Field(min_length=1, max_length=32_000)
    memory: bool = False
    tools: bool = False


def _normalized_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base_url must be an absolute HTTP(S) URL without embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    return candidate


def _seed_connection() -> LumenConnection | None:
    key_path = os.environ.get("LUMEN_CONSOLE_SEED_KEY_FILE", "").strip()
    if not key_path:
        return None
    try:
        credential = Path(key_path).read_text().strip()
    except OSError:
        return None
    if not credential.startswith("sk-afgl-"):
        return None
    return LumenConnection(
        base_url=os.environ.get("LUMEN_CONSOLE_DEFAULT_URL", "http://lumen-api:8012").rstrip("/"),
        auth_mode="api_key",
        credential=credential,
        project_id=None,
    )


def create_app(database_path: str | Path | None = None) -> FastAPI:
    store = LocalUserStore(database_path or os.environ.get("LUMEN_CONSOLE_DB", ".lumen-console/users.sqlite"))
    vault = ConnectionVault()
    app = FastAPI(title="Lumen Console", version="1.0.0")
    app.state.user_store = store
    app.state.connection_vault = vault
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    def current_user(session_token: str | None = Cookie(default=None, alias=_COOKIE_NAME)) -> LocalUser:
        user = store.session_user(session_token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다")
        return user

    def current_session(session_token: str | None = Cookie(default=None, alias=_COOKIE_NAME)) -> str:
        if session_token is None or store.session_user(session_token) is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="로그인이 필요합니다")
        return session_token

    def session_connection(session_token: str) -> LumenConnection | None:
        connection = vault.get(session_token)
        if connection is None and (connection := _seed_connection()) is not None:
            vault.set(session_token, connection)
        return connection

    def configured_connection(session_token: str = Depends(current_session)) -> LumenConnection:
        connection = session_connection(session_token)
        if connection is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Lumen 연결을 먼저 설정하세요")
        return connection

    def set_session(response: Response, token: str) -> None:
        response.set_cookie(
            _COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            secure=os.environ.get("LUMEN_CONSOLE_SECURE_COOKIES", "false").lower() == "true",
            max_age=7 * 24 * 60 * 60,
            path="/",
        )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html")

    @app.get("/api/bootstrap")
    async def bootstrap_status() -> dict[str, bool]:
        return {"needs_bootstrap": store.needs_bootstrap()}

    @app.post("/api/auth/bootstrap", status_code=status.HTTP_201_CREATED)
    async def bootstrap(body: CredentialsBody, response: Response) -> dict[str, str]:
        try:
            user = store.bootstrap(body.username, body.password)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        set_session(response, store.create_session(user))
        return {"username": user.username}

    @app.post("/api/auth/login")
    async def login(body: CredentialsBody, response: Response) -> dict[str, str]:
        user = store.authenticate(body.username, body.password)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="사용자 이름 또는 비밀번호가 올바르지 않습니다")
        set_session(response, store.create_session(user))
        return {"username": user.username}

    @app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(response: Response, session_token: str | None = Cookie(default=None, alias=_COOKIE_NAME)) -> None:
        store.delete_session(session_token)
        vault.discard(session_token)
        response.delete_cookie(_COOKIE_NAME, path="/")

    @app.get("/api/auth/me")
    async def me(user: LocalUser = Depends(current_user)) -> dict[str, str]:
        return {"username": user.username}

    @app.get("/api/connection")
    async def connection_status(
        session_token: str = Depends(current_session), user: LocalUser = Depends(current_user)
    ) -> dict[str, str | bool | None]:
        connection = session_connection(session_token)
        return {
            "username": user.username,
            "connected": connection is not None,
            "base_url": connection.base_url if connection else None,
            "auth_mode": connection.auth_mode if connection else None,
            "project_id": connection.project_id if connection else None,
        }

    @app.put("/api/connection")
    async def configure_connection(body: ConnectionBody, session_token: str = Depends(current_session)) -> dict[str, str | None]:
        try:
            base_url = _normalized_base_url(body.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        if body.auth_mode == "api_key" and not body.credential.startswith("sk-afgl-"):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="API key must start with sk-afgl-")
        vault.set(
            session_token,
            LumenConnection(
                base_url=base_url,
                auth_mode=body.auth_mode,
                credential=body.credential,
                project_id=body.project_id.strip() if body.project_id else None,
            ),
        )
        return {"base_url": base_url, "auth_mode": body.auth_mode, "project_id": body.project_id}

    async def upstream_json(
        connection: LumenConnection,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> object:
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.request(
                    method,
                    f"{connection.base_url}{path}",
                    headers={**connection.headers(), **(headers or {})},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Lumen 연결에 실패했습니다") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise HTTPException(status_code=response.status_code, detail=detail)
        return response.json()

    @app.get("/api/models")
    async def models(connection: LumenConnection = Depends(configured_connection)) -> object:
        return await upstream_json(connection, "GET", "/v1/chat/models")

    @app.post("/api/chat/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(body: ChatBody, connection: LumenConnection = Depends(configured_connection)) -> object:
        return await upstream_json(
            connection,
            "POST",
            "/v1/temp-completions",
            payload={
                "parts": [{"type": "text", "text": body.text}],
                "model_id": body.model_id,
                "features": {
                    "memory": body.memory,
                    "tool_policy": {"mode": "agent_default" if body.tools else "none"},
                },
            },
            headers={"Idempotency-Key": str(uuid4())},
        )

    @app.get("/api/chat/runs/{run_id}/events")
    async def replay_events(
        run_id: str,
        request: Request,
        connection: LumenConnection = Depends(configured_connection),
    ) -> StreamingResponse:
        last_event_id = request.headers.get("Last-Event-ID")

        async def relay() -> AsyncIterator[bytes]:
            headers = {**connection.headers(), "Accept": "text/event-stream"}
            if last_event_id:
                headers["Last-Event-ID"] = last_event_id
            try:
                async with (
                    httpx.AsyncClient(timeout=None, trust_env=False) as client,
                    client.stream("GET", f"{connection.base_url}/v1/runs/{run_id}/events", headers=headers) as upstream,
                ):
                    if upstream.status_code >= 400:
                        detail = (await upstream.aread()).decode("utf-8", errors="replace")
                        yield f"event: error\ndata: {detail}\n\n".encode()
                        return
                    async for chunk in upstream.aiter_raw():
                        yield chunk
            except httpx.HTTPError:
                yield b"event: error\ndata: Lumen connection failed\n\n"

        return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
