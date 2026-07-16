"""One-time repair for an existing user's immutable timezone.

Usage:

    uv run python -m kokoro_link.cli.repair_user_timezone \
        --email alice@example.com --timezone Asia/Taipei
    uv run python -m kokoro_link.cli.repair_user_timezone \
        --email alice@example.com --timezone Asia/Taipei --apply

The command is intentionally outside normal profile APIs. User timezone
is an identity-time setting; this tool exists only for upgrade repair
when an existing deployment was backfilled with the wrong timezone.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy import select

from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id
from kokoro_link.infrastructure.persistence.engine import (
    build_async_engine,
    build_session_factory,
)
from kokoro_link.infrastructure.persistence.models import OperatorProfileRow

_LOGGER = logging.getLogger(__name__)


def _timezone_arg(raw: str) -> str:
    try:
        return normalise_timezone_id(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _email_arg(raw: str) -> str:
    value = raw.strip().lower()
    if not value:
        raise argparse.ArgumentTypeError("email cannot be empty")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair one existing operator_profiles.timezone_id value. "
            "Default mode is a dry run; pass --apply to write."
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", help="operator_profiles.id to repair.")
    target.add_argument(
        "--email",
        type=_email_arg,
        help="operator_profiles.email to repair, case-insensitive.",
    )
    parser.add_argument(
        "--timezone",
        required=True,
        type=_timezone_arg,
        help="Target IANA timezone id, e.g. Asia/Taipei.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually update the row. Omit for dry-run output only.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database or not settings.database_url:
        _LOGGER.error("DATABASE_URL / KOKORO_DATABASE_URL must be set.")
        return 2

    engine = build_async_engine(settings.database_url)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            stmt = select(OperatorProfileRow)
            if args.user_id:
                stmt = stmt.where(OperatorProfileRow.id == args.user_id)
            else:
                stmt = stmt.where(OperatorProfileRow.email == args.email)

            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                target = args.user_id or args.email
                _LOGGER.error("No operator profile found for %s", target)
                return 1

            before = row.timezone_id or "UTC"
            after = args.timezone
            if before == after:
                print(
                    f"No change needed for {row.id} ({row.email or 'no email'}): "
                    f"timezone is already {after}."
                )
                return 0

            if not args.apply:
                print(
                    f"DRY RUN: would change {row.id} ({row.email or 'no email'}) "
                    f"timezone_id from {before} to {after}. "
                    "Re-run with --apply to write."
                )
                return 0

            row.timezone_id = after
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            print(
                f"Updated {row.id} ({row.email or 'no email'}) "
                f"timezone_id from {before} to {after}."
            )
            return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
