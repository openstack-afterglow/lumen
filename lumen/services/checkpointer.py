"""Lifecycle-owned encrypted LangGraph PostgreSQL checkpointer.

A checkpointer is optional for ordinary text chat. It is intentionally not
replaced with an in-memory saver in production: a process-local checkpoint
would make an approval/resume appear durable when it is not.
"""

from __future__ import annotations

import asyncio
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from psycopg.errors import InsufficientPrivilege
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from lumen.crypto import derive_encryption_subkey

logger = logging.getLogger(__name__)
_DOMAIN = b"chat_checkpoint"
_CIPHER_NAME = "afterglow-aesgcm-v1"


class ChatCheckpointCipher:
    """LangGraph serializer cipher using the app's domain-separated AES-GCM key."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def encrypt(self, plaintext: bytes) -> tuple[str, bytes]:
        nonce = os.urandom(12)
        return _CIPHER_NAME, nonce + AESGCM(self._key).encrypt(nonce, plaintext, _DOMAIN)

    def decrypt(self, ciphername: str, ciphertext: bytes) -> bytes:
        if ciphername != _CIPHER_NAME or len(ciphertext) < 13:
            raise ValueError("invalid chat checkpoint ciphertext")
        nonce, payload = ciphertext[:12], ciphertext[12:]
        return AESGCM(self._key).decrypt(nonce, payload, _DOMAIN)


class ChatCheckpointer:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._pool: AsyncConnectionPool | None = None
        self._saver: AsyncPostgresSaver | None = None

    @property
    def saver(self) -> AsyncPostgresSaver | None:
        return self._saver

    @property
    def available(self) -> bool:
        return self._saver is not None

    async def start(self, dsn: str) -> bool:
        """Open and initialize the saver once; failure leaves it unavailable."""

        if not dsn:
            return False
        async with self._lock:
            if self._saver is not None:
                return True
            pool = AsyncConnectionPool(
                conninfo=dsn,
                kwargs={"autocommit": True, "row_factory": dict_row},
                min_size=1,
                max_size=4,
                timeout=3,
                open=False,
            )
            try:
                await pool.open(wait=True, timeout=3)
                serializer = EncryptedSerializer(ChatCheckpointCipher(derive_encryption_subkey(_DOMAIN)))
                saver = AsyncPostgresSaver(pool, serde=serializer)
                await saver.setup()
            except InsufficientPrivilege:
                await pool.close()
                logger.warning(
                    "chat checkpoint PostgreSQL role lacks schema privileges; grant USAGE, CREATE on its target schema before enabling approval tools"
                )
                return False
            except Exception:
                await pool.close()
                logger.warning("chat checkpoint PostgreSQL is unavailable; approval tools stay disabled", exc_info=True)
                return False
            self._pool = pool
            self._saver = saver
            logger.info("chat checkpoint PostgreSQL initialized")
            return True

    async def close(self) -> None:
        async with self._lock:
            self._saver = None
            pool, self._pool = self._pool, None
            if pool is not None:
                await pool.close()


chat_checkpointer = ChatCheckpointer()
