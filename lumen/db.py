"""Lumen database lifecycle: availability circuit and ORM metadata only.

Engine/pool/driver creation and connection-error classification belong to the
selected ``lumen.database`` plugin (``DatabasePlugin.open`` /
``DatabaseHandle``). This module owns only the process-wide availability
circuit breaker and the SQLAlchemy declarative base shared by every model.
"""

from __future__ import annotations

import logging
import sys
import time

from lumen_plugin_api.database import DatabaseConfig, DatabaseHandle
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

_logger = logging.getLogger(__name__)
_handle: DatabaseHandle | None = None

_db_unhealthy_until: float = 0.0
_default_unhealthy_seconds: int = 15


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
    """Open the selected database plugin's handle; synchronous, no I/O."""
    global _handle, _default_unhealthy_seconds
    from lumen.plugins.registry import get_plugin

    _default_unhealthy_seconds = unhealthy_seconds
    _handle = get_plugin("database").open(
        DatabaseConfig(
            url=database_url,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_timeout=connect_timeout,
            pool_timeout=pool_timeout,
        )
    )


def get_session_factory() -> async_sessionmaker[AsyncSession] | None:
    return _handle.session_factory if _handle is not None else None


def is_db_available() -> bool:
    if _handle is None:
        return False
    if time.monotonic() < _db_unhealthy_until:
        return False
    return True


def is_db_configured() -> bool:
    return _handle is not None


def is_connection_error(error: BaseException | None) -> bool:
    if error is None or _handle is None:
        return False
    return _handle.is_connection_error(error)


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
    if _handle is None:
        return False
    try:
        return await _handle.check()
    except Exception as exc:
        mark_db_unhealthy(exc)
        return False


async def close_db() -> None:
    global _handle
    if _handle is not None:
        await _handle.close()
        _handle = None
