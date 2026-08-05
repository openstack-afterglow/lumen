"""Lumen SQLAlchemy async database lifecycle."""

from __future__ import annotations

import logging
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

_logger = logging.getLogger(__name__)
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

_db_unhealthy_until: float = 0.0
_default_unhealthy_seconds: int = 15
_CONNECTION_ERROR_CODES = frozenset({2003, 2006, 2013, 2014, 2055})


class Base(DeclarativeBase):
    pass


def init_db(
    database_url: str,
    pool_size: int = 20,
    max_overflow: int = 10,
    connect_timeout: int = 10,
    pool_timeout: int = 30,
    unhealthy_seconds: int = 15,
) -> None:
    global _engine, _session_factory, _default_unhealthy_seconds
    _default_unhealthy_seconds = unhealthy_seconds
    connect_args: dict = {"connect_timeout": connect_timeout}
    _engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    return _session_factory


def is_db_available() -> bool:
    if _engine is None:
        return False
    if time.monotonic() < _db_unhealthy_until:
        return False
    return True


def is_db_configured() -> bool:
    return _engine is not None


def is_connection_error(error: BaseException | None) -> bool:
    if error is None:
        return False
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return True
    args = getattr(error, "args", None)
    if args and isinstance(args[0], int) and args[0] in _CONNECTION_ERROR_CODES:
        return True
    return False


def mark_db_unhealthy(error: BaseException | None = None, seconds: int | None = None) -> bool:
    error = error if error is not None else sys.exception()
    if error is not None and not is_connection_error(error):
        return False
    global _db_unhealthy_until
    duration = seconds if seconds is not None else _default_unhealthy_seconds
    _db_unhealthy_until = time.monotonic() + duration
    _logger.warning("DB connection failure detected; marking DB unavailable for %d seconds", duration)
    return True


async def check_db() -> bool:
    if _engine is None:
        return False
    try:
        async with _engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        mark_db_unhealthy(exc)
        return False


async def close_db() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
    _session_factory = None
