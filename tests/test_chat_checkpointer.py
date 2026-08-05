from lumen.services.checkpointer import ChatCheckpointCipher, ChatCheckpointer


async def test_empty_checkpoint_dsn_keeps_basic_chat_uncheckpointed():
    manager = ChatCheckpointer()

    assert await manager.start("") is False
    assert manager.available is False
    assert manager.saver is None


async def test_schema_privilege_failure_closes_pool_and_disables_checkpointing(monkeypatch):
    from psycopg.errors import InsufficientPrivilege

    class Pool:
        closed = False

        async def open(self, **_kwargs):
            raise InsufficientPrivilege("permission denied for schema checkpoints")

        async def close(self):
            self.closed = True

    pool = Pool()
    monkeypatch.setattr(
        "lumen.services.checkpointer.AsyncConnectionPool",
        lambda **_kwargs: pool,
    )
    manager = ChatCheckpointer()

    assert await manager.start("postgresql://checkpoint.example/afterglow") is False
    assert pool.closed is True
    assert manager.available is False


def test_checkpoint_cipher_uses_authenticated_domain_separation():
    cipher = ChatCheckpointCipher(b"k" * 32)
    name, encrypted = cipher.encrypt(b"hidden provider state")

    assert cipher.decrypt(name, encrypted) == b"hidden provider state"
    assert encrypted != b"hidden provider state"
