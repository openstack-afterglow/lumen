from datetime import UTC, datetime, timedelta


def month_start(now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime | None = None) -> datetime:
    """ISO week: Monday 00:00 UTC."""
    now = now or datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())
