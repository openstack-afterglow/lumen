"""Register Lumen as an OpenStack SDK service."""

from lumen_sdk.client import Client
from lumen_sdk.service import LumenService

__version__ = "0.1.6"


def register(conn):
    """Enable Lumen and return ``conn.lumen``.

    ``lumen`` is not an official OpenStack service type, so
    ``CloudRegion.has_service()`` would otherwise fall back to
    ``os_service_types.is_official()`` and report it disabled — every
    ``conn.lumen.*`` access would then raise ``ServiceDisabledException``
    instead of issuing a request. ``enable_service`` must run before
    ``add_service`` attaches the proxy.
    """
    conn.config.enable_service("lumen")
    conn.add_service(LumenService())
    return conn.lumen


__all__ = ["Client", "LumenService", "register"]
