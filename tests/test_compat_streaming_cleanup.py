"""Pending upstream reads and settlement never escape the ASGI cleanup owner."""
from __future__ import annotations

import asyncio

import anyio
import pytest

from lumen.api.compat.streaming import events_with_ping

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("billing_error", [False, True])
async def test_cancelled_pending_read_drains_and_settles_before_propagating(billing_error):
    read_started = asyncio.Event()
    finish_read = asyncio.Event()
    billing_started = asyncio.Event()
    finish_billing = asyncio.Event()
    calls = 0
    read_cancelled = False
    settled = False

    async def upstream():
        nonlocal calls, read_cancelled, settled
        calls += 1
        try:
            yield {"type": "delta", "text": "first"}
            read_started.set()
            try:
                await finish_read.wait()
            except asyncio.CancelledError:
                read_cancelled = True
                raise
            yield {"type": "delta", "text": "second"}
        finally:
            billing_started.set()
            await finish_billing.wait()
            settled = True
            if billing_error:
                raise RuntimeError("settlement failed")

    events = events_with_ping(upstream(), ping_seconds=60)
    assert await anext(events) == {"type": "delta", "text": "first"}
    consumer = asyncio.create_task(anext(events))
    try:
        await asyncio.wait_for(read_started.wait(), 2)
        consumer.cancel()
        await asyncio.sleep(0)
        assert not consumer.done()
        assert not read_cancelled
        finish_read.set()
        await asyncio.wait_for(billing_started.wait(), 2)
        assert not consumer.done()
        assert not settled
        finish_billing.set()
        if billing_error:
            with pytest.raises(RuntimeError, match="settlement failed"):
                await consumer
        else:
            with pytest.raises(asyncio.CancelledError):
                await consumer
        assert settled
        assert calls == 1
        assert not read_cancelled
    finally:
        finish_read.set()
        finish_billing.set()
        await asyncio.gather(consumer, return_exceptions=True)
        await events.aclose()


@pytest.mark.parametrize("billing_error", [False, True])
async def test_close_between_yields_awaits_billing_without_starting_another_read(billing_error):
    billing_started = asyncio.Event()
    finish_billing = asyncio.Event()
    calls = 0
    reads_after_yield = 0

    async def upstream():
        nonlocal calls, reads_after_yield
        calls += 1
        try:
            yield {"type": "delta", "text": "first"}
            reads_after_yield += 1
            yield {"type": "delta", "text": "second"}
        finally:
            billing_started.set()
            await finish_billing.wait()
            if billing_error:
                raise RuntimeError("settlement failed")

    events = events_with_ping(upstream())
    await anext(events)
    closing = asyncio.create_task(events.aclose())
    try:
        await asyncio.wait_for(billing_started.wait(), 2)
        assert not closing.done()
        assert reads_after_yield == 0
        finish_billing.set()
        if billing_error:
            with pytest.raises(RuntimeError, match="settlement failed"):
                await closing
        else:
            await closing
        assert calls == 1
        assert reads_after_yield == 0
    finally:
        finish_billing.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_anyio_disconnect_scope_cannot_interrupt_pending_settlement():
    billing_started = asyncio.Event()
    finish_billing = asyncio.Event()
    completed = asyncio.Event()
    entered = asyncio.Event()
    scope_holder = []

    async def upstream():
        try:
            yield {"type": "delta", "text": "first"}
            await asyncio.sleep(0)
            yield {"type": "delta", "text": "second"}
        finally:
            billing_started.set()
            await finish_billing.wait()
            completed.set()

    async def owner():
        with anyio.CancelScope() as scope:
            scope_holder.append(scope)
            events = events_with_ping(upstream(), ping_seconds=60)
            try:
                await anext(events)
                entered.set()
                await anyio.sleep_forever()
            finally:
                await events.aclose()

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        scope_holder[0].cancel()
        await asyncio.wait_for(billing_started.wait(), 2)
        assert not task.done()
        assert not completed.is_set()
        finish_billing.set()
        await task
        assert completed.is_set()
    finally:
        finish_billing.set()
        await asyncio.gather(task, return_exceptions=True)
