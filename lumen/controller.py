"""Standalone fenced cloud resource controller process."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import logging
import os
import ssl
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID
from fastapi import FastAPI
from uvicorn import Config, Server
from uvicorn.protocols.http.h11_impl import H11Protocol

from lumen.config import get_settings
from lumen.db import close_db, init_db
from lumen.plugins.host import build_host
from lumen.plugins.registry import get_registry
from lumen.services.infrastructure.bootstrap import _ca, make_bootstrap_router
from lumen.services.infrastructure.config import RuntimeConfig
from lumen.services.infrastructure.controller import ResourceController
from lumen.services.infrastructure.dispatch import make_dispatch_router
from lumen.services.infrastructure.nova import NovaProvider
from lumen.services.infrastructure.transport import resolve_operator_key
from lumen.services.infrastructure.zun import ZunProvider

logger = logging.getLogger(__name__)


class InternalH11Protocol(H11Protocol):
    """Expose only TLS-verified client URI identity to the internal ASGI app."""

    def connection_made(self, transport):
        super().connection_made(transport)
        ssl_object = transport.get_extra_info("ssl_object")
        peer = ssl_object.getpeercert(binary_form=True) if ssl_object else None
        identity = None
        if peer:
            try:
                certificate = x509.load_der_x509_certificate(peer)
                usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
                names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                uris = names.get_values_for_type(x509.UniformResourceIdentifier)
                if ExtendedKeyUsageOID.CLIENT_AUTH in usage and len(uris) == 1:
                    identity = uris[0]
            except (ValueError, x509.ExtensionNotFound):
                pass
        app = self.app

        async def verified_app(scope, receive, send):
            if identity is not None:
                scope["lumen_client_identity"] = identity
                scope["lumen_client_fingerprint"] = hashlib.sha256(peer).hexdigest()
            await app(scope, receive, send)

        self.app = verified_app


def internal_app(config: RuntimeConfig) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(make_bootstrap_router(config, resolve_operator_key(config.dispatch_key)))
    app.include_router(make_dispatch_router(config))
    return app


def internal_server(config: RuntimeConfig) -> Server:
    if config.tls is None:
        raise ValueError("controller TLS material is required")
    options = Config(
        internal_app(config), host=config.listen_host, port=config.listen_port,
        http=InternalH11Protocol, ws="none", proxy_headers=False,
        ssl_certfile=config.tls.cert_file, ssl_keyfile=config.tls.key_file,
        ssl_ca_certs=config.tls.ca_file, ssl_cert_reqs=ssl.CERT_OPTIONAL,
        access_log=False, timeout_keep_alive=5,
    )
    return Server(options)


def build_providers(config: RuntimeConfig) -> dict[str, object]:
    """Construct one least-privilege provider per enabled pool from its cloud profile.

    A ``LUMEN_CONTROLLER_PROVIDER_FACTORY=module.path:callable`` override is honored
    only in ``development``; production always builds real Nova/Zun providers so a
    stray environment variable can never substitute a fake backend in a real cloud.
    """
    override = os.environ.get("LUMEN_CONTROLLER_PROVIDER_FACTORY", "").strip()
    if override:
        if os.environ.get("AFTERGLOW_ENV", "development").strip().lower() != "development":
            logger.warning("refusing LUMEN_CONTROLLER_PROVIDER_FACTORY outside development")
        else:
            module_name, _, attribute = override.partition(":")
            factory = getattr(importlib.import_module(module_name), attribute)
            result = factory(config)
            if isinstance(result, dict):
                return {pool.name: result[pool.name] if pool.name in result else result[pool.backend]
                        for pool in config.pools if pool.enabled}
            return {pool.name: result for pool in config.pools if pool.enabled}
    providers: dict[str, object] = {}
    if config.tls is None:
        raise ValueError("controller TLS material is required")
    ca_pem = Path(config.tls.ca_file).read_text(encoding="ascii")
    for pool in config.pools:
        if not pool.enabled:
            continue
        cloud = config.profile(pool.cloud_profile_id)
        provider = NovaProvider if pool.backend == "nova" else ZunProvider
        providers[pool.name] = provider(cloud, pool, config.deployment_id,
                                        controller_url=config.controller_url, controller_ca_pem=ca_pem,
                                        **({"managed_networks": config.managed_networks} if pool.backend == "nova" else {}))
    return providers


async def run_controller(config: RuntimeConfig, providers: dict[str, object], *,
                         stop: asyncio.Event | None = None,
                         db_pool_size: int = 20, db_overflow: int = 10) -> None:
    """Preflight providers and serve bootstrap/dispatch alongside reconciliation."""
    if config.tls is None:
        raise ValueError("controller TLS material is required")
    if config.tls.operator_client_cert_file and (
        os.path.samefile(config.tls.operator_client_cert_file, config.tls.cert_file)
        or os.path.samefile(config.tls.operator_client_key_file, config.tls.key_file)
    ):
        raise ValueError("controller probe identity must be separate from listener identity")
    _ca(config)
    server_cert = x509.load_pem_x509_certificate(Path(config.tls.cert_file).read_bytes())
    server_key = serialization.load_pem_private_key(Path(config.tls.key_file).read_bytes(), password=None)
    if (server_cert.issuer != x509.load_pem_x509_certificate(Path(config.tls.ca_file).read_bytes()).subject
            or server_cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
            != server_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)):
        raise ValueError("controller listener certificate does not match configured CA/key")
    for pool in config.pools:
        if pool.enabled:
            if pool.name not in providers:
                raise ValueError(f"missing provider for pool {pool.name}")
            await asyncio.to_thread(providers[pool.name].preflight, pool)
    controller = ResourceController(config, providers, owner=f"{os.uname().nodename}-{uuid.uuid4()}",
                                    db_pool_size=db_pool_size, db_overflow=db_overflow)
    server = internal_server(config)
    server_task = asyncio.create_task(server.serve())
    reconcile_task = asyncio.create_task(controller.run())

    async def _watch() -> None:
        if stop is not None:
            await stop.wait()
            controller.stop()
            server.should_exit = True

    watcher = asyncio.create_task(_watch()) if stop is not None else None
    try:
        done, _ = await asyncio.wait({server_task, reconcile_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if server_task in done and not server.started and stop is None:
            raise RuntimeError("controller TLS listener did not start")
    finally:
        controller.stop()
        server.should_exit = True
        if watcher is not None:
            watcher.cancel()
        await asyncio.gather(server_task, reconcile_task, return_exceptions=True)


async def _serve() -> None:
    settings = get_settings()
    config = settings.runtime_config
    if not config.enabled:
        raise RuntimeError("runtime controller is disabled")
    if not settings.database_url:
        raise RuntimeError("controller requires DATABASE_URL")
    init_db(settings.database_url, pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow)
    registry = get_registry()
    try:
        registry.load()
        await registry.start(build_host())
        providers = build_providers(config)
        await run_controller(config, providers, db_pool_size=settings.database_pool_size,
                             db_overflow=settings.database_max_overflow)
    finally:
        try:
            await registry.close()
        finally:
            await close_db()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve())
