"""Environment provider bootstrap through the real provider repository (sqlite)."""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from lumen.models.chat_db import LlmModel, LlmProvider
from lumen.scripts import seed_providers as seeder
from lumen.services.providers import repository
from lumen.services.providers.errors import ProviderValidationError

_TEXT = {"provider": "openai", "model_name": "gpt-4.1-mini", "model_kind": "text",
         "input_price_per_million": "1", "output_price_per_million": "3"}
_IMAGE = {"provider": "openai", "model_name": "gpt-image-1", "model_kind": "image",
          "media_pricing": {"image_variants": {"1024x1024:high": "0.05"}}}


class _Transaction(AbstractAsyncContextManager):
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, *_):
        (self.session.sync.commit if exc_type is None else self.session.sync.rollback)()
        return False


class _AsyncSession(AbstractAsyncContextManager):
    def __init__(self, sync: Session, ids: dict[type, int]):
        self.sync, self.ids = sync, ids

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.sync.close()
        return False

    def begin(self):
        return _Transaction(self)

    async def execute(self, statement):
        return self.sync.execute(statement)

    async def get(self, entity, identity):
        return self.sync.get(entity, identity)

    def add(self, row):
        self.sync.add(row)

    async def flush(self):
        # sqlite does not autoincrement BIGINT primary keys.
        for row in self.sync.new:
            if type(row) in self.ids and row.id is None:
                row.id = self.ids[type(row)]
                self.ids[type(row)] += 1
        self.sync.flush()


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch):
    # Never depend on or read operator credentials present in the shell.
    for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", seeder.MODELS_ENV):
        monkeypatch.delenv(name, raising=False)
    engine = create_engine("sqlite:///:memory:")
    LlmProvider.__table__.create(engine)
    LlmModel.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE chat_runs (id TEXT PRIMARY KEY, status TEXT)")
        connection.exec_driver_sql("CREATE TABLE chat_run_providers (run_id TEXT, provider_id INTEGER, model_id INTEGER)")
    sync_factory = sessionmaker(engine, expire_on_commit=False)
    ids = {LlmProvider: 1, LlmModel: 100}
    monkeypatch.setattr(repository, "_require_db", lambda: lambda: _AsyncSession(sync_factory(), ids))
    monkeypatch.setattr("lumen.services.assets.asset_pipeline_available", lambda: True)
    yield sync_factory, ids
    engine.dispose()


def _add_provider(db, **fields):
    sync_factory, ids = db
    row = LlmProvider(id=ids[LlmProvider], auth_mode="api_key", margin_multiplier=1, is_active=True, **fields)
    ids[LlmProvider] += 1
    with sync_factory.begin() as session:
        session.add(row)
    return row.id


def _snapshot(db):
    with db[0]() as session:
        providers = [(p.name, p.provider_type, p.api_base, p.api_key_env, p.encrypted_api_key)
                     for p in session.scalars(select(LlmProvider).order_by(LlmProvider.id))]
        models = [(m.provider_id, m.model_name, m.model_kind, m.input_price, m.media_pricing)
                  for m in session.scalars(select(LlmModel).order_by(LlmModel.id))]
    return providers, models


def _models(monkeypatch, *entries):
    monkeypatch.setenv(seeder.MODELS_ENV, json.dumps(list(entries)))


@pytest.mark.asyncio
async def test_present_keys_create_env_bound_providers_idempotently(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "secret-openai")
    monkeypatch.setenv("GEMINI_API_KEY", "secret-gemini")
    _models(monkeypatch, _TEXT, {**_TEXT, "provider": "gemini", "model_name": "gemini-2.5-flash"})

    rows = await seeder.seed_environment_providers()
    assert {name: (row["api_key_env"], row["api_key_source"]) for name, row in rows.items()} == {
        "openai": ("OPENAI_API_KEY", "environment"), "gemini": ("GEMINI_API_KEY", "environment")}
    first = _snapshot(db)
    assert [p[4] for p in first[0]] == [None, None]
    assert "secret-" not in repr((first, rows))
    assert {(m[1], m[2]) for m in first[1]} == {("gpt-4.1-mini", "text"), ("gemini-2.5-flash", "text")}

    assert (await seeder.seed_environment_providers()).keys() == rows.keys()
    assert _snapshot(db) == first


@pytest.mark.asyncio
async def test_missing_key_creates_no_provider_or_its_models(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    _models(monkeypatch, _TEXT, {**_TEXT, "provider": "gemini", "model_name": "gemini-2.5-flash"})
    assert set(await seeder.seed_environment_providers()) == {"openai"}
    providers, models = _snapshot(db)
    assert [p[0] for p in providers] == ["openai"]
    assert [m[1] for m in models] == ["gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_no_model_config_creates_no_models(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    assert set(await seeder.seed_environment_providers()) == {"openai"}
    assert _snapshot(db)[1] == []


@pytest.mark.asyncio
async def test_database_key_and_existing_model_prices_win(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "seed-secret")
    provider_id = _add_provider(db, name="openai", provider_type="openai", encrypted_api_key="ciphertext")
    await repository.create_model(provider_id=provider_id, model_name="gpt-4.1-mini",
                                  input_price_per_million="99", output_price_per_million="99")
    before = _snapshot(db)
    _models(monkeypatch, _TEXT, {**_TEXT, "model_name": "gpt-4.1"})
    rows = await seeder.seed_environment_providers()
    assert (rows["openai"]["api_key_source"], rows["openai"]["api_key_env"]) == ("database", None)
    providers, models = _snapshot(db)
    assert providers == before[0]
    assert models[0] == before[1][0]
    assert [m[1] for m in models[1:]] == ["gpt-4.1"]


@pytest.mark.parametrize("fields", [
    {"api_key_env": "ADMIN_OPENAI_KEY"},  # admin binding kept even though its variable is empty
    {"api_base": "https://proxy.example/v1"},  # never send the key to an unpaired endpoint
])
@pytest.mark.asyncio
async def test_admin_bindings_and_custom_endpoints_are_not_rebound(monkeypatch, db, fields):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    _add_provider(db, name="openai", provider_type="openai", **fields)
    _models(monkeypatch, _TEXT)
    before = _snapshot(db)
    assert (await seeder.seed_environment_providers())["openai"]["has_api_key"] is False
    assert _snapshot(db) == before


@pytest.mark.asyncio
async def test_unowned_canonical_slot_is_bound_then_models_created(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    _add_provider(db, name="openai", provider_type="openai", api_base="https://api.openai.com/v1")
    _models(monkeypatch, _TEXT)
    row = (await seeder.seed_environment_providers())["openai"]
    assert (row["api_key_env"], row["api_key_source"]) == ("OPENAI_API_KEY", "environment")
    providers, models = _snapshot(db)
    assert providers[0][2] == "https://api.openai.com/v1"
    assert [m[1] for m in models] == ["gpt-4.1-mini"]


@pytest.mark.asyncio
async def test_existing_prefixed_gemini_model_is_not_duplicated(monkeypatch, db):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    provider_id = _add_provider(db, name="gemini", provider_type="gemini", api_key_env="GEMINI_API_KEY")
    await repository.create_model(provider_id=provider_id, model_name="gemini/gemini-2.5-flash",
                                  input_price_per_million="5", output_price_per_million="5")
    _models(monkeypatch, {**_TEXT, "provider": "gemini", "model_name": "gemini-2.5-flash"})
    before = _snapshot(db)
    await seeder.seed_environment_providers()
    assert _snapshot(db) == before


@pytest.mark.parametrize(("entry", "message"), [
    ({**_IMAGE, "model_name": "gpt-image-unknown"}, "unsupported image provider or model"),
    ({**_IMAGE, "media_pricing": {"image_variants": {"1024x1024:high": "0"}}}, "exact image variant price"),
    ({**_IMAGE, "media_pricing": {"image_per_unit": "0.1"}}, "exact size:quality"),
    ({**_IMAGE, "media_pricing": {"image_variants": {"1024x1024:2k": "0.05"}}}, "unsupported image quality"),
    ({**_IMAGE, "media_pricing": {}}, "explicit media_pricing"),
    ({"provider": "openai", "model_name": "tts-1", "model_kind": "tts",
      "media_pricing": {"audio_per_character": "0.01"}}, "exact configured audio price"),
    ({"provider": "openai", "model_name": "gpt-realtime", "model_kind": "realtime",
      "media_pricing": {"realtime_input_per_minute": "0.01"}}, "exact configured realtime price"),
    ({**_TEXT, "model_name": "gpt-4.1", "output_price_per_million": None}, "explicit input and output"),
    ({**_TEXT, "model_name": "chatgpt/gpt-5"}, "구독"),
    ({**_IMAGE, "provider": "anthropic"}, "provider must be one of"),
    ({**_IMAGE, "api_key": "private-value"}, "must contain exactly"),
    ({k: v for k, v in _IMAGE.items() if k != "model_kind"}, "unsupported model_kind"),
    ({**_TEXT, "model_name": "gpt-4.1-mini"}, "duplicates"),
])
@pytest.mark.asyncio
async def test_invalid_model_config_fails_before_any_write(monkeypatch, db, entry, message):
    monkeypatch.setenv("OPENAI_API_KEY", "private-value")
    _models(monkeypatch, _TEXT, entry)
    with pytest.raises(ProviderValidationError, match=message) as error:
        await seeder.seed_environment_providers()
    assert "private-value" not in str(error.value)
    assert _snapshot(db) == ([], [])


@pytest.mark.asyncio
async def test_malformed_json_and_custom_base_media_fail_before_any_write(monkeypatch, db):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv(seeder.MODELS_ENV, "{not json")
    with pytest.raises(ProviderValidationError, match="not valid JSON"):
        await seeder.seed_environment_providers()
    assert _snapshot(db) == ([], [])

    _add_provider(db, name="openai", provider_type="openai", api_key_env="OPENAI_API_KEY",
                  api_base="https://proxy.example/v1")
    _models(monkeypatch, _IMAGE)
    before = _snapshot(db)
    with pytest.raises(ProviderValidationError, match="direct API-key route"):
        await seeder.seed_environment_providers()
    assert _snapshot(db) == before


@pytest.mark.parametrize(("entry", "gate"), [
    ({"provider": "gemini", "model_name": "gemini-3.1-flash-image", "model_kind": "image",
      "media_pricing": {"image_variants": {"1536x1024:2k": "0.15"}}}, "image_output"),
    ({"provider": "gemini", "model_name": "gemini-3.8-flash-tts", "model_kind": "tts",
      "media_pricing": {"audio_output_per_second": "0.001"}}, "audio_output"),
    ({"provider": "gemini", "model_name": "gemini-3.8-live", "model_kind": "realtime",
      "media_pricing": {"realtime_input_per_minute": "0.01", "realtime_output_per_minute": "0.02"}}, "audio_input"),
])
@pytest.mark.asyncio
async def test_valid_media_model_is_active_and_available(monkeypatch, db, entry, gate):
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    _models(monkeypatch, entry)
    await seeder.seed_environment_providers()
    (model,) = await repository.list_models()
    assert (model["model_name"], model["model_kind"], model["is_active"]) == (
        entry["model_name"], entry["model_kind"], True)
    assert model["effective_capabilities"]["feature_gates"][gate]["available"] is True


@pytest.mark.asyncio
async def test_cli_closes_db_on_failure_and_prints_no_secret(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("DATABASE_URL", "mysql+aiomysql://lumen@db/lumen")
    monkeypatch.setattr(seeder, "init_db", lambda url: calls.append(("init", url)))

    async def close():
        calls.append(("close",))

    async def bootstrap():
        calls.append(("bootstrap",))
        raise ProviderValidationError("failed safely")

    monkeypatch.setattr(seeder, "close_db", close)
    monkeypatch.setattr(seeder, "_bootstrap", bootstrap)
    with pytest.raises(ProviderValidationError, match="failed safely"):
        await seeder.seed()
    assert calls == [("init", "mysql+aiomysql://lumen@db/lumen"), ("bootstrap",), ("close",)]
    assert capsys.readouterr().out == ""
