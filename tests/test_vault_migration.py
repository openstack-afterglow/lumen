"""Legacy Vault import keeps deployment and directory failures recoverable."""

import json
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


@pytest.mark.parametrize(("failure", "reason", "minutes"), [
    (vault_migration.DisabledProject(), "origin_disabled", 15),
    (vault_migration.UnresolvedProject(), "origin_unresolved", 60),
    (TimeoutError("directory timeout"), "directory_unavailable", 1),
])
async def test_origin_deferrals_stay_pending_and_log_once_per_origin(monkeypatch, caplog, failure, reason, minutes):
    finished = []
    rows = {
        note_id: SimpleNamespace(id=note_id, project_id="origin-project", vault_migration_status=None,
                                 vault_migration_retry_at=None)
        for note_id in (4, 5)
    }
    session = _Session(targets=[(4, "origin-project"), (5, "origin-project")])

    async def get(_model, note_id, **_kwargs):
        return rows[note_id]

    session.get = get

    def lookup(_project):
        raise failure

    original = vault_migration._finish_one

    async def finish(*args, **kwargs):
        finished.append(kwargs)
        return await original.__wrapped__(*args, **kwargs)

    monkeypatch.setattr(vault_migration, "get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(vault_migration, "derive_encryption_subkey", lambda _domain: b"k" * 32)
    monkeypatch.setattr(vault_migration, "_native_domain", lookup)
    monkeypatch.setattr(vault_migration, "_finish_one", finish)
    before = datetime.now(UTC)

    assert await vault_migration.migrate_batch() == 2
    assert finished == [{"blocked": None, "deferral": reason}] * 2
    for row in rows.values():
        assert row.vault_migration_status is None
        assert before + timedelta(minutes=minutes) <= row.vault_migration_retry_at
        assert row.vault_migration_retry_at <= datetime.now(UTC) + timedelta(minutes=minutes)
    warnings = [record.getMessage() for record in caplog.records if record.levelname == "WARNING"]
    assert warnings == [
        f"vault_import_deferred project_id=origin-project reason={reason} rows=2 retry_after_seconds={minutes * 60}"
    ]
    assert "memory_id" not in caplog.text


async def test_decrypt_failure_defers_without_logging_content(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger=vault_migration.logger.name)
    row = SimpleNamespace(id=6, project_id=None, vault_migration_status=None, vault_migration_retry_at=None,
                          content="ciphertext-fixture")
    session = _Session(row=row)

    async def undecryptable(*_args):
        raise vault_migration.InvalidTag

    session.begin_nested = lambda: _Session()
    monkeypatch.setattr(vault_migration, "_import_one", undecryptable)

    assert await vault_migration._finish_one.__wrapped__(lambda: session, 6, None, None, blocked=None) == "decrypt_failed"
    assert row.vault_migration_status is None and row.vault_migration_retry_at is not None
    assert "vault_import_deferred_row memory_id=6 reason=decrypt_failed" in caplog.text
    assert not [record for record in caplog.records if record.levelname == "WARNING"]
    assert "ciphertext-fixture" not in caplog.text


def test_status_command_prints_read_only_summary(monkeypatch, capsys):
    calls = []
    summary = {"statuses": {"imported": 2, "pending": 1}, "pending": {"due": 0, "deferred": 1},
               "deferred_origins": [{"project_id": "origin-project", "rows": 1, "next_attempt_at": None}]}

    async def status():
        calls.append("status")
        return summary

    async def close():
        calls.append("close")

    monkeypatch.setattr(vault_migration, "get_settings", lambda: SimpleNamespace(database_url="mysql+aiomysql://fixture"))
    monkeypatch.setattr(vault_migration, "init_db", lambda url, **kwargs: calls.append((url, kwargs)))
    monkeypatch.setattr(vault_migration, "migration_status", status)
    monkeypatch.setattr(vault_migration, "close_db", close)

    assert vault_migration.main(["status"]) == 0
    assert json.loads(capsys.readouterr().out) == summary
    assert calls == [("mysql+aiomysql://fixture", {"pool_size": 1, "max_overflow": 0}), "status", "close"]


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
