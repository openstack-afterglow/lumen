"""Copy Lumen-owned chat state from Afterglow during a maintenance window.

Apply Lumen migrations first, stop both chat writers, then run this module.
Source credentials are accepted only through ``AFTERGLOW_SOURCE_DATABASE_URL``;
the destination comes from Lumen's normal configuration. The operation is
restart-safe: missing rows are inserted, byte-for-byte identical rows are
retained, and divergent or destination-only rows abort the cutover.

    AFTERGLOW_SOURCE_DATABASE_URL=... python -m lumen.scripts.cutover --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from lumen.config import get_settings


class CutoverError(RuntimeError):
    """The cutover cannot proceed without risking Lumen data loss."""


# Dependency order is intentional for the destination schema's foreign keys.
_TABLE_NAMES = (
    "chat_agents",
    "chat_api_keys",
    "chat_assets",
    "chat_custom_tools",
    "chat_extension_packages",
    "chat_git_credentials",
    "chat_mcp_oauth_connections",
    "chat_mcp_oauth_requests",
    "chat_mcp_servers",
    "chat_mcp_credentials",
    "chat_memories",
    "chat_memory_owner_locks",
    "chat_scheduler_leases",
    "chat_skills",
    "chat_temp_threads",
    "chat_usage_logs",
    "user_wallets",
    "chat_workspaces",
    "llm_providers",
    "chat_code_workspaces",
    "chat_commands",
    "chat_extension_package_installs",
    "chat_memory_outbox",
    "llm_models",
    "chat_code_workspace_assets",
    "chat_conversations",
    "chat_extension_package_components",
    "chat_messages",
    "chat_input_derivations",
    "chat_memory_provenance",
    "chat_message_assets",
    "chat_runs",
    "chat_context_checkpoints",
    "chat_jobs",
    "chat_run_assets",
    "chat_run_events",
    "chat_run_interactions",
    "chat_run_providers",
    "chat_run_segments",
    "chat_run_turns",
    "chat_tool_approvals",
)


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[str, ...]
    primary_key: tuple[str, ...]


def _mysql_async_url(raw_url: str, *, label: str) -> URL:
    if not raw_url.strip():
        raise CutoverError(f"{label} database URL is required")
    url = make_url(raw_url)
    if url.get_backend_name() != "mysql":
        raise CutoverError("Lumen cutover supports MySQL/MariaDB databases only")
    if url.drivername in {"mysql", "mysql+asyncmy"}:
        url = url.set(drivername="mysql+aiomysql")
    if url.drivername != "mysql+aiomysql":
        raise CutoverError("database URL must use mysql, mysql+asyncmy, or mysql+aiomysql")
    return url


def _quoted(name: str) -> str:
    if name not in _TABLE_NAMES:
        raise CutoverError("unknown Lumen table")
    return f"`{name}`"


def _canonical(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"[", "{"}:
            try:
                return _canonical(json.loads(stripped))
            except json.JSONDecodeError:
                pass
        return value
    if isinstance(value, Mapping):
        return {key: _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical(item) for item in value]
    return value


def _rows_equal(left: Mapping[str, Any], right: Mapping[str, Any], columns: Sequence[str]) -> bool:
    return all(_canonical(left.get(column)) == _canonical(right.get(column)) for column in columns)


async def _table_spec(connection: AsyncConnection, name: str) -> TableSpec:
    columns = tuple(
        row["Field"] for row in (await connection.execute(text(f"SHOW COLUMNS FROM {_quoted(name)}"))).mappings()
    )
    if not columns:
        raise CutoverError(f"required table is missing: {name}")
    primary_key_rows = (
        await connection.execute(text(f"SHOW KEYS FROM {_quoted(name)} WHERE Key_name = 'PRIMARY'"))
    ).mappings()
    primary_key = tuple(row["Column_name"] for row in sorted(primary_key_rows, key=lambda row: row["Seq_in_index"]))
    if not primary_key:
        raise CutoverError(f"required table has no primary key: {name}")
    return TableSpec(name=name, columns=columns, primary_key=primary_key)


async def _rows(connection: AsyncConnection, spec: TableSpec) -> list[dict[str, Any]]:
    columns = ", ".join(f"`{column}`" for column in spec.columns)
    ordering = ", ".join(f"`{column}`" for column in spec.primary_key)
    result = await connection.execute(text(f"SELECT {columns} FROM {_quoted(spec.name)} ORDER BY {ordering}"))
    return [dict(row) for row in result.mappings()]


def _row_key(row: Mapping[str, Any], spec: TableSpec) -> tuple[Any, ...]:
    return tuple(row[column] for column in spec.primary_key)


def _copy_plan(
    spec: TableSpec, source_rows: Sequence[Mapping[str, Any]], destination_rows: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    source = {_row_key(row, spec): row for row in source_rows}
    destination = {_row_key(row, spec): row for row in destination_rows}
    if len(source) != len(source_rows) or len(destination) != len(destination_rows):
        raise CutoverError(f"duplicate primary key detected in {spec.name}")
    if set(destination) - set(source):
        raise CutoverError(f"destination {spec.name} has rows absent from Afterglow")

    inserts: list[dict[str, Any]] = []
    for key, source_row in source.items():
        destination_row = destination.get(key)
        if destination_row is None:
            inserts.append(dict(source_row))
        elif not _rows_equal(source_row, destination_row, spec.columns):
            raise CutoverError(f"conflicting {spec.name} row during cutover")
    return tuple(inserts)


async def _insert(connection: AsyncConnection, spec: TableSpec, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns = ", ".join(f"`{column}`" for column in spec.columns)
    values = ", ".join(f":{column}" for column in spec.columns)
    await connection.execute(text(f"INSERT INTO {_quoted(spec.name)} ({columns}) VALUES ({values})"), list(rows))


async def _plan(
    source: AsyncConnection, destination: AsyncConnection
) -> tuple[tuple[TableSpec, tuple[dict[str, Any], ...]], ...]:
    plans: list[tuple[TableSpec, tuple[dict[str, Any], ...]]] = []
    for name in _TABLE_NAMES:
        source_spec = await _table_spec(source, name)
        destination_spec = await _table_spec(destination, name)
        if source_spec.columns != destination_spec.columns or source_spec.primary_key != destination_spec.primary_key:
            raise CutoverError(f"source and destination schema differ for {name}")
        plans.append(
            (
                source_spec,
                _copy_plan(source_spec, await _rows(source, source_spec), await _rows(destination, destination_spec)),
            )
        )
    return tuple(plans)


async def cutover(*, source_database_url: str, destination_database_url: str, apply: bool) -> dict[str, int]:
    source_engine = create_async_engine(_mysql_async_url(source_database_url, label="source"), pool_pre_ping=True)
    destination_engine = create_async_engine(
        _mysql_async_url(destination_database_url, label="destination"), pool_pre_ping=True
    )
    try:
        async with source_engine.connect() as source, destination_engine.connect() as destination:
            plans = await _plan(source, destination)
            summary = {spec.name: len(rows) for spec, rows in plans}
            if not apply:
                return summary

            await destination.rollback()
            async with destination.begin():
                await destination.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
                try:
                    for spec, rows in plans:
                        await _insert(destination, spec, rows)
                finally:
                    await destination.execute(text("SET FOREIGN_KEY_CHECKS = 1"))

            verification = await _plan(source, destination)
            if any(rows for _, rows in verification):
                raise CutoverError("Lumen cutover verification found uncopied rows")
            return summary
    finally:
        await source_engine.dispose()
        await destination_engine.dispose()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Copy Lumen-owned state from Afterglow")
    parser.add_argument("--apply", action="store_true", help="perform the verified copy")
    return parser.parse_args()


async def _main() -> int:
    args = _args()
    summary = await cutover(
        source_database_url=os.environ.get("AFTERGLOW_SOURCE_DATABASE_URL", ""),
        destination_database_url=get_settings().database_url,
        apply=args.apply,
    )
    print(json.dumps({"apply": args.apply, "tables": summary}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
