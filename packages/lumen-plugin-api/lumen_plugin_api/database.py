"""Optional SQLAlchemy contract; domain transactions remain host-owned."""
from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .contracts import ContractModel, Plugin


class DatabaseConfig(ContractModel):
    url: SecretStr
    pool_size: int = Field(default=20, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    connect_timeout: int = Field(default=10, ge=1)
    pool_timeout: int = Field(default=30, ge=1)


class DatabaseHandle(Protocol):
    contract: Literal["mariadb_transactional_v1"]
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    async def check(self) -> bool: ...
    async def close(self) -> None: ...
    def is_connection_error(self, error: BaseException | None) -> bool: ...


class DatabasePlugin(Plugin, Protocol):
    def open(self, config: DatabaseConfig) -> DatabaseHandle: ...
