"""Legacy Vault import keeps deployment and directory failures recoverable."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from lumen.services import vault_migration


class _Session:
    def __init__(self, row=None, targets=()):
        self.row = row
        self.targets = targets

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def begin(self):
        return self

    async def execute(self, _statement):
        return SimpleNamespace(all=lambda: list(self.targets))

    async def get(self, *_args, **_kwargs):
        return self.row


async def test_invalid_encryption_key_pauses_batch_without_touching_rows(monkeypatch, caplog):
    opened = []

    def unavailable(_domain):
        raise ValueError("Lumen encryption key is not set")

    monkeypatch.setattr(vault_migration, "get_session_factory", lambda: lambda: opened.append(True))
    monkeypatch.setattr(vault_migration, "derive_encryption_subkey", unavailable)

    assert await vault_migration.migrate_batch() == 0
    assert opened == []
    assert "encryption key is missing or invalid" in caplog.text
    assert await vault_migration.process_one.__wrapped__(owner="worker") is False


async def test_disabled_origin_stays_pending_with_backoff(monkeypatch):
    finished = []
    row = SimpleNamespace(id=4, project_id="disabled-project", vault_migration_status=None,
                          vault_migration_retry_at=None)
    session = _Session(row=row, targets=[(4, "disabled-project")])

    def disabled(_project):
        raise vault_migration.DisabledProject

    original = vault_migration._finish_one

    async def finish(*args, **kwargs):
        finished.append(kwargs)
        await original.__wrapped__(*args, **kwargs)

    monkeypatch.setattr(vault_migration, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(vault_migration, "derive_encryption_subkey", lambda _domain: b"k" * 32)
    monkeypatch.setattr(vault_migration, "_native_domain", disabled)
    monkeypatch.setattr(vault_migration, "_finish_one", finish)
    before = datetime.now(UTC)

    assert await vault_migration.migrate_batch() == 1
    assert finished == [{"blocked": None, "retry_after": timedelta(minutes=15)}]
    assert row.vault_migration_status is None
    assert before + timedelta(minutes=14) < row.vault_migration_retry_at <= datetime.now(UTC) + timedelta(minutes=15)


@pytest.mark.parametrize("error", [ValueError("malformed row"), vault_migration.UnresolvedProject()])
async def test_row_data_errors_still_seal_only_that_row(monkeypatch, error):
    row = SimpleNamespace(id=5, project_id=None, vault_migration_status=None, vault_migration_retry_at=None)
    session = _Session(row=row)

    async def import_fails(*_args):
        raise error

    session.begin_nested = lambda: _Session()
    monkeypatch.setattr(vault_migration, "_import_one", import_fails)

    await vault_migration._finish_one.__wrapped__(lambda: session, 5, None, None, blocked=None)
    assert row.vault_migration_status == "invalid"
