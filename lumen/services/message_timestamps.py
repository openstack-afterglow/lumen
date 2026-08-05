"""UTC and browser-local timestamp construction for persisted chat messages."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_client_timezone(value: str | None) -> str | None:
    """Return a valid IANA timezone name, rejecting unknown client input."""
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("client_timezone must be a valid IANA timezone") from exc
    return value


def message_timestamps(
    client_timezone: str | None, *, now: datetime | None = None
) -> tuple[datetime, datetime | None, str | None]:
    """Create one UTC instant and its wall-clock representation for a client timezone."""
    source = now or datetime.now(UTC)
    created_at = source.replace(tzinfo=UTC) if source.tzinfo is None else source.astimezone(UTC)
    timezone_name = validate_client_timezone(client_timezone)
    if timezone_name is None:
        return created_at, None, None
    local = created_at.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None)
    return created_at, local, timezone_name
