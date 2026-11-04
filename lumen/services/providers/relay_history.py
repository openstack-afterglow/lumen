"""Secret-free, execution-owner-local dispatch receipts for settled usage history.

This is settled-usage evidence, not a connection monitor or an all-attempt journal.
No historical destination is reconstructed from mutable provider configuration.
"""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from inspect import isasyncgenfunction
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

MAX_RECEIPTS = 64
OPERATIONS = frozenset(
    {"chat", "responses", "messages", "count_tokens", "embeddings", "search", "image", "tts", "stt", "realtime"}
)

_receipts: ContextVar[list | None] = ContextVar("provider_relay_receipts", default=None)


class _ReceiptIterator:
    def __init__(self, source, rows):
        self.source, self.rows = source, rows

    def __getattr__(self, name):
        return getattr(self.source, name)

    def __aiter__(self):
        return self

    async def __anext__(self):
        token = _receipts.set(self.rows)
        try:
            return await self.source.__anext__()
        finally:
            _receipts.reset(token)

    async def aclose(self):
        token = _receipts.set(self.rows)
        try:
            await self.source.aclose()
        finally:
            _receipts.reset(token)


def receipt_owner(function):
    """A new collector per HTTP/worker execution; never mutate frozen auth refs.

    Returned streams retain their own collector through iteration and close, without
    leaving context installed in the caller between awaits. Replays start empty.
    """
    if isasyncgenfunction(function):

        @wraps(function)
        def generator(*args, **kwargs):
            return _ReceiptIterator(function(*args, **kwargs), [])

        return generator

    @wraps(function)
    async def wrapped(*args, **kwargs):
        rows = []
        token = _receipts.set(rows)
        try:
            result = await function(*args, **kwargs)
            return _ReceiptIterator(result, rows) if hasattr(result, "__anext__") else result
        finally:
            _receipts.reset(token)

    return wrapped


def current_receipts(event_id=None):
    rows = _receipts.get() or ()
    if event_id is not None:
        for row in rows:
            row.setdefault("_settlement_event", event_id)
    return [
        {key: value for key, value in row.items() if key != "_settlement_event"}
        for row in rows
        if event_id is None or row.get("_settlement_event") == event_id
    ]


def safe_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https", "ws", "wss"} or not url.hostname:
            return None
        host = f"[{url.hostname}]" if ":" in url.hostname else url.hostname
        if url.port is not None:
            host += f":{url.port}"
        return urlunsplit((url.scheme, host, url.path, "", ""))
    except ValueError:
        return None


def record_dispatch(ref, *, endpoint, model, operation, status_code=None):
    """Called only at the owned transport boundary, never from a DTO or current config."""
    rows = _receipts.get()
    if rows is None or ref is None or operation not in OPERATIONS:
        return
    identity = {
        "provider_id": ref.get("provider_id"),
        "account_id": ref.get("account_id"),
        "credential_id": ref.get("credential_id"),
        "generation": ref.get("generation"),
    }
    if any(type(value) is not int for value in identity.values()):
        return
    if len(rows) >= MAX_RECEIPTS:
        return
    rows.append(
        {
            **identity,
            "attempt_id": str(uuid4()),
            "endpoint": safe_endpoint(endpoint),
            "model": model if isinstance(model, str) and len(model) <= 190 else None,
            "operation": operation,
            "status": "response_received" if status_code is not None else "dispatched",
            "status_code": status_code if type(status_code) is int and 100 <= status_code <= 599 else None,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
    )


def public_receipts(rows, *, provider_id, account_id, credential_id, generation):
    """Whitelist persisted JSON; unknown/old/malformed attribution stays absent."""
    result = []
    for row in rows[:MAX_RECEIPTS] if isinstance(rows, list) else ():
        if not isinstance(row, dict) or any(
            row.get(key) != value
            for key, value in {
                "provider_id": provider_id,
                "account_id": account_id,
                "credential_id": credential_id,
                "generation": generation,
            }.items()
        ):
            continue
        if row.get("operation") not in OPERATIONS or row.get("status") not in {"dispatched", "response_received"}:
            continue
        attempt_id = row.get("attempt_id")
        try:
            from uuid import UUID

            attempt_id = str(UUID(attempt_id))
            recorded_at = datetime.fromisoformat(row["recorded_at"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        result.append(
            {
                "attempt_id": attempt_id,
                "endpoint": safe_endpoint(row.get("endpoint")),
                "model": row.get("model") if isinstance(row.get("model"), str) and len(row["model"]) <= 190 else None,
                "operation": row["operation"],
                "status": row["status"],
                "status_code": row.get("status_code")
                if type(row.get("status_code")) is int and 100 <= row["status_code"] <= 599
                else None,
                "recorded_at": recorded_at,
            }
        )
    return result
