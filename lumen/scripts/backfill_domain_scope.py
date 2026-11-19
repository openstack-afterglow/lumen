"""Backfill legacy user/domain scope after migration 036 using configured Keystone admin authority."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from lumen.config import get_settings
from lumen.db import close_db, init_db
from lumen.services.domain_scope_backfill import backfill_domain_scope


async def _run(database_url: str):
    init_db(database_url)
    try:
        return await backfill_domain_scope()
    finally:
        await close_db()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--apply", action="store_true", required=True)
    args = parser.parse_args()
    database_url = args.database_url or get_settings().database_url
    if not database_url:
        raise SystemExit("database URL is required")
    try:
        report = asyncio.run(_run(database_url))
    except Exception as exc:
        raise SystemExit(f"domain scope backfill stopped ({type(exc).__name__})") from None
    print(json.dumps(asdict(report)))
    if not report.lock_acquired:
        raise SystemExit("another backfill holds the scope lock")
    if report.projects_unresolved:
        raise SystemExit("unresolved projects remain origin-only")
    if report.items_failed:
        raise SystemExit("some extension rows remain origin-only after a storage conflict")


if __name__ == "__main__":  # pragma: no cover
    main()
