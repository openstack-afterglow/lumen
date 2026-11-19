"""Embedding calls settle one input-token ledger row and wallet delta against real MariaDB."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from provider_fixtures import configured_provider
from sqlalchemy import delete, func, select

from lumen.db import close_db, get_session_factory, init_db
from lumen.models.chat_db import ChatUsageLog, LlmModel, LlmProvider, UserWallet
from lumen.services import credit, embeddings, provider_client

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("current_project_authority")]


async def _register(factory, nonce: str, *, provider_type: str, model_name: str) -> int:
    async with factory() as session, session.begin():
        provider = configured_provider(name=f"embedding-{nonce}", api_provider=f"emb-{nonce[:16]}", provider_type=provider_type,
                                       is_active=True, margin_multiplier=Decimal("2"))
        session.add(provider)
        await session.flush()
        session.add(LlmModel(
            provider_id=provider.id, model_name=model_name, model_kind="embedding",
            input_price=Decimal("0.0000002"), output_price=Decimal("0"), is_active=True,
        ))
        return provider.id


@pytest.mark.parametrize("provider_type, model_name, usage, estimated, dimensions", [
    ("openai", "text-embedding-3-small", {"prompt_tokens": 1234, "total_tokens": 1234}, False, 2),
    ("gemini", "gemini-embedding-001", None, True, 128),
])
async def test_embedding_call_persists_one_usage_row_and_wallet_charge(
        monkeypatch, provider_type, model_name, usage, estimated, dimensions):
    init_db(os.environ["DATABASE_URL"], pool_size=2, max_overflow=0)
    factory = get_session_factory()
    assert factory is not None
    nonce = uuid.uuid4().hex
    user_id, project_id = f"embedding-user-{nonce}", f"embedding-project-{nonce}"
    provider_calls = []
    provider_id = None
    provider_name = f"emb-{nonce[:16]}"

    async def provider(**kwargs):
        provider_calls.append(kwargs)
        return SimpleNamespace(data=[{"embedding": [0.25] * dimensions}], usage=usage)

    monkeypatch.setattr(provider_client, "aembedding", provider)
    try:
        provider_id = await _register(factory, nonce, provider_type=provider_type, model_name=model_name)
        route = await embeddings.resolve(model_name, provider=provider_name)
        result = await embeddings.embed(
            route=route, inputs=["hello embeddings"], user_id=user_id, project_id=project_id,
            api_key_id=None, required_scopes=("native:runs:write",), dimensions=dimensions,
        )
        assert len(provider_calls) == 1
        assert provider_calls[0]["dimensions"] == dimensions
        assert len(result["embeddings"][0]["embedding"]) == dimensions
        tokens = result["usage"]["prompt_tokens"]
        assert tokens > 0 and result["usage"]["estimated"] is estimated
        if not estimated:
            assert tokens == 1234
        expected_raw = Decimal(tokens) * Decimal("0.0000002")
        async with factory() as session:
            rows = (await session.execute(select(ChatUsageLog).where(
                ChatUsageLog.user_id == user_id, ChatUsageLog.project_id == project_id))).scalars().all()
            wallet = await session.get(UserWallet, user_id)
        assert len(rows) == 1
        row = rows[0]
        assert (row.model_name, row.prompt_tokens, row.completion_tokens, row.source) == (
            model_name, tokens, 0, "api")
        assert row.raw_cost == expected_raw
        # Independent expectation: the registered provider margin is 2x.
        expected_credited = credit.credits_for_cost(expected_raw, Decimal("2"))
        assert row.credited_cost == expected_credited > 0
        assert Decimal(str(result["credited_cost"])) == expected_credited
        assert wallet is not None and wallet.used_quota_this_month == row.credited_cost
    finally:
        async with factory() as session, session.begin():
            await session.execute(delete(ChatUsageLog).where(ChatUsageLog.user_id == user_id))
            await session.execute(delete(UserWallet).where(UserWallet.user_id == user_id))
            if provider_id is not None:
                await session.execute(delete(LlmProvider).where(LlmProvider.id == provider_id))
            assert (await session.execute(select(func.count()).select_from(ChatUsageLog).where(
                ChatUsageLog.user_id == user_id))).scalar_one() == 0
        await close_db()
