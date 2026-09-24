"""MariaDB async SQLAlchemy database plugin for Lumen.

Engine/pool/driver ownership and MariaDB connection-error classification live
here. Domain transactions, locking, schema and the availability circuit
breaker remain entirely with the host (``lumen.db``); this wheel only knows
how to open a pooled async engine and check/close it.
"""

from __future__ import annotations

from dataclasses import dataclass

from lumen_plugin_api.contracts import PluginHost, PluginManifest
from lumen_plugin_api.database import DatabaseConfig
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# MySQL/MariaDB client error codes that indicate a lost/refused connection, not a query fault.
_CONNECTION_ERROR_CODES = frozenset({2003, 2006, 2013, 2014, 2055})


@dataclass(frozen=True, kw_only=True)
class MariaDbHandle:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    contract: str = "mariadb_transactional_v1"

    async def check(self) -> bool:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True

    async def close(self) -> None:
        await self.engine.dispose()

    def is_connection_error(self, error: BaseException | None) -> bool:
        """SQLAlchemy wraps the driver exception in ``.orig``; the real MariaDB code lives there,
        not in the wrapper's ``args[0]`` (which SQLAlchemy replaces with a formatted message)."""
        if error is None:
            return False
        for candidate in (error, getattr(error, "orig", None)):
            if candidate is None:
                continue
            if isinstance(candidate, (TimeoutError, ConnectionError, OSError)):
                return True
            args = getattr(candidate, "args", None)
            if args and isinstance(args[0], int) and args[0] in _CONNECTION_ERROR_CODES:
                return True
        return False


class MariaDbDatabase:
    """MariaDB transactional relational connection plugin; not a NoSQL adapter."""

    manifest = PluginManifest(
        id="mariadb",
        kind="database",
        version="0.1.0",
        required_capabilities=(),
    )

    async def start(self, host: PluginHost) -> None:
        return None

    async def close(self) -> None:
        return None

    def open(self, config: DatabaseConfig) -> MariaDbHandle:
        """Create the pooled async engine synchronously; no connection is made yet."""
        engine = create_async_engine(
            config.url.get_secret_value(),
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout,
            pool_pre_ping=True,
            connect_args={"connect_timeout": config.connect_timeout},
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        return MariaDbHandle(engine=engine, session_factory=session_factory)


def create_plugin() -> MariaDbDatabase:
    return MariaDbDatabase()
