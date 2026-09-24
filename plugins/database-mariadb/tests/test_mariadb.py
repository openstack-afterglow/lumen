"""Conformance for the packaged MariaDB async database provider."""
from __future__ import annotations

import pytest
from lumen_database_mariadb import create_plugin
from lumen_plugin_api.contracts import PluginHost
from lumen_plugin_api.database import DatabaseConfig
from lumen_plugin_api.testing import check_entry_point, check_lifecycle, check_manifest
from sqlalchemy.exc import OperationalError

_UNREACHABLE_URL = "mysql+aiomysql://unreachable-host.invalid:3306/db"


def test_factory_manifest_and_entry_point():
    plugin = create_plugin()
    assert check_manifest(plugin, expected_kind="database").id == "mariadb"
    check_entry_point("lumen-database-mariadb", plugin)


@pytest.mark.asyncio
async def test_lifecycle_starts_and_closes_two_instances():
    await check_lifecycle(create_plugin, PluginHost(configuration={}))


def test_open_creates_the_handle_contract_without_any_network_io():
    """``open`` must be pure/synchronous; no socket exists until a query actually runs."""
    plugin = create_plugin()
    handle = plugin.open(DatabaseConfig(url=_UNREACHABLE_URL))
    assert handle.contract == "mariadb_transactional_v1"
    assert handle.engine is not None
    assert handle.session_factory is not None


@pytest.mark.parametrize(
    "error,expected",
    [
        (TimeoutError("timeout"), True),
        (ConnectionError("refused"), True),
        (OSError("io"), True),
        (type("MysqlError", (Exception,), {})(2003, "cannot connect"), True),
        (type("MysqlError", (Exception,), {})(2006, "server has gone away"), True),
        (type("MysqlError", (Exception,), {})(2013, "lost connection during query"), True),
        (type("MysqlError", (Exception,), {})(2014, "commands out of sync"), True),
        (type("MysqlError", (Exception,), {})(2055, "lost connection to server"), True),
        (type("MysqlError", (Exception,), {})(1062, "duplicate entry"), False),
        (ValueError("not a connection error"), False),
        (None, False),
    ],
)
def test_connection_error_classification(error, expected):
    plugin = create_plugin()
    handle = plugin.open(DatabaseConfig(url=_UNREACHABLE_URL))
    assert handle.is_connection_error(error) is expected


class _DriverError(Exception):
    """Stand-in for the aiomysql/pymysql exception SQLAlchemy wraps as ``.orig``."""


def test_wrapped_sqlalchemy_operational_error_unwraps_orig_for_connection_codes():
    """The real MariaDB code lives on ``.orig``, not on the wrapper's own ``args[0]`` message."""
    plugin = create_plugin()
    handle = plugin.open(DatabaseConfig(url=_UNREACHABLE_URL))
    driver_error = _DriverError(2003, "Can't connect to MySQL server")
    wrapped = OperationalError("SELECT 1", {}, driver_error)
    assert isinstance(wrapped.args[0], str)  # the wrapper's own args[0] is a message, never the int code
    assert wrapped.orig is driver_error
    assert handle.is_connection_error(wrapped) is True


def test_wrapped_sqlalchemy_operational_error_does_not_flag_non_connection_codes():
    plugin = create_plugin()
    handle = plugin.open(DatabaseConfig(url=_UNREACHABLE_URL))
    driver_error = _DriverError(1062, "Duplicate entry")
    wrapped = OperationalError("INSERT INTO t VALUES (1)", {}, driver_error)
    assert handle.is_connection_error(wrapped) is False
