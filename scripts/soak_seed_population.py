"""Synthetic soak population seeder (HOSTED_CORE_SCALING §13 Phase 2).

Seeds a deterministic virtual operator/character population for the Phase 2
synthetic soak: many operators across near-midnight-diverse timezones, a
frozen fraction, and a proactive-disabled fraction. Everything is created
through the REAL container services (auth registration, ``CharacterService``,
the repository freeze path) so relationship seeding, FK ownership and freeze
semantics are exactly what production would see — the soak then exercises the
shadow queue against a realistic work set.

The random draw is seeded (``--seed``) so a given ``(--prefix, --seed, ...)``
always produces the same population; combined with a fixed ``--prefix`` this
also lets the interaction driver re-derive the operators' login credentials
without any secret ever being written to a log or the summary.

Idempotency: operators/characters are named by ``--prefix``. A re-run whose
first operator email already exists FAILS CLEANLY with a message telling you to
use a fresh database or a different ``--prefix`` (chosen over "silently skip"
so a half-seeded population never masquerades as complete).

Prod-safety guard (red line): the seeder refuses to touch a database unless you
either pass ``--database-url`` explicitly OR pass
``--i-know-this-is-a-soak-environment`` (which then permits reading
``DATABASE_URL`` from the environment). A prod URL is never picked up by
accident.

Usage::

    uv run python scripts/soak_seed_population.py \
        --database-url postgresql+asyncpg://kokoro:...@127.0.0.1:5432/soak \
        --operators 6 --characters-per 15 --seed 20260720
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

_DEFAULT_TIMEZONES = (
    "Asia/Taipei",        # UTC+8
    "America/New_York",   # UTC-5/-4
    "Europe/Berlin",      # UTC+1/+2
    "Pacific/Auckland",   # UTC+12/+13 — first past midnight
    "Asia/Kolkata",       # UTC+5:30 — half-hour offset edge
    "Pacific/Honolulu",   # UTC-10 — last to midnight
)
_DEFAULT_PREFIX = "soak"
_DEFAULT_PASSWORD_SALT = "yuralume-soak"


# --------------------------------------------------------------------------- #
# Deterministic plan (pure)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OperatorPlan:
    key: str
    email: str
    display_name: str
    timezone_id: str


@dataclass(frozen=True, slots=True)
class CharacterPlan:
    operator_key: str
    name: str
    proactive_enabled: bool
    freeze: bool


@dataclass(frozen=True, slots=True)
class SeedPlan:
    prefix: str
    operators: tuple[OperatorPlan, ...]
    characters: tuple[CharacterPlan, ...]

    @property
    def frozen_count(self) -> int:
        return sum(1 for c in self.characters if c.freeze)

    @property
    def proactive_disabled_count(self) -> int:
        return sum(1 for c in self.characters if not c.proactive_enabled)

    def timezone_distribution(self) -> dict[str, int]:
        counts: Counter[str] = Counter(op.timezone_id for op in self.operators)
        return dict(sorted(counts.items()))


def soak_password(prefix: str, seed: int) -> str:
    """Derive the deterministic soak login password from ``(prefix, seed)``.

    The seeder and the interaction driver both call this so the driver can log
    in without any credential being written to a log. It is a well-known soak
    secret by construction — never reuse a soak database for anything real."""
    digest = hashlib.sha256(
        f"{_DEFAULT_PASSWORD_SALT}:{prefix}:{seed}".encode("utf-8"),
    ).hexdigest()
    return f"Soak-{digest[:24]}"


def operator_email(prefix: str, index: int) -> str:
    # example.com is reserved for documentation (RFC 2606), so a soak address
    # cannot collide with a real mailbox.  Unlike the special-use ``.invalid``
    # TLD it is accepted by pydantic EmailStr, which guards the real login route
    # the interaction driver exercises.
    return f"{prefix}-op{index}@example.com"


def build_seed_plan(
    *,
    prefix: str = _DEFAULT_PREFIX,
    operators: int,
    characters_per: int,
    timezones: Sequence[str] = _DEFAULT_TIMEZONES,
    freeze_fraction: float,
    proactive_disabled_fraction: float,
    seed: int,
) -> SeedPlan:
    """Build the deterministic seed plan. Pure — no I/O, fully unit-testable."""
    if operators < 1:
        raise ValueError("--operators must be >= 1")
    if characters_per < 1:
        raise ValueError("--characters-per must be >= 1")
    if not timezones:
        raise ValueError("at least one timezone is required")
    if not 0.0 <= freeze_fraction <= 1.0:
        raise ValueError("--freeze-fraction must be in [0, 1]")
    if not 0.0 <= proactive_disabled_fraction <= 1.0:
        raise ValueError("--proactive-disabled-fraction must be in [0, 1]")

    op_plans = tuple(
        OperatorPlan(
            key=f"{prefix}-op{i}",
            email=operator_email(prefix, i),
            display_name=f"{prefix} operator {i}",
            # Round-robin assignment guarantees an even, deterministic spread
            # across the timezone list (near-midnight diversity by design).
            timezone_id=timezones[i % len(timezones)],
        )
        for i in range(operators)
    )

    total = operators * characters_per
    rng = random.Random(seed)
    frozen_n = round(total * freeze_fraction)
    disabled_n = round(total * proactive_disabled_fraction)
    # Independent draws so a character may be both frozen AND proactive-disabled
    # (a realistic overlap the shadow validation must still classify correctly).
    frozen_idx = set(rng.sample(range(total), frozen_n)) if frozen_n else set()
    disabled_idx = (
        set(rng.sample(range(total), disabled_n)) if disabled_n else set()
    )

    char_plans: list[CharacterPlan] = []
    idx = 0
    for i in range(operators):
        for j in range(characters_per):
            char_plans.append(CharacterPlan(
                operator_key=f"{prefix}-op{i}",
                name=f"{prefix}-op{i}-char{j}",
                proactive_enabled=idx not in disabled_idx,
                freeze=idx in frozen_idx,
            ))
            idx += 1

    return SeedPlan(
        prefix=prefix,
        operators=op_plans,
        characters=tuple(char_plans),
    )


# --------------------------------------------------------------------------- #
# Execution (real services)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SeedSummary:
    operators: int
    characters: int
    frozen: int
    proactive_disabled: int
    timezones: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "operators": self.operators,
            "characters": self.characters,
            "frozen": self.frozen,
            "proactive_disabled": self.proactive_disabled,
            "timezones": self.timezones,
        }


# ``create_operator(email, display_name, timezone_id, password) -> operator_id``
CreateOperator = Callable[..., Awaitable[str]]


async def seed_population(
    plan: SeedPlan,
    *,
    create_operator: CreateOperator,
    character_service: object,
    character_repository: object,
    password: str,
    now: datetime | None = None,
) -> SeedSummary:
    """Materialise ``plan`` through the real creation paths.

    ``create_operator`` is injected so the CLI can use the full auth
    registration path while a test can supply a lighter real-repo path; both
    persist a genuine operator row that the character FK requires."""
    from kokoro_link.application.dto.character import CreateCharacterRequest
    from kokoro_link.domain.entities.character import FREEZE_REASON_MANUAL

    resolved_now = now or datetime.now(timezone.utc)
    op_ids: dict[str, str] = {}
    for op in plan.operators:
        op_ids[op.key] = await create_operator(
            email=op.email,
            display_name=op.display_name,
            timezone_id=op.timezone_id,
            password=password,
        )

    frozen = 0
    proactive_disabled = 0
    for char in plan.characters:
        request = CreateCharacterRequest(
            name=char.name,
            personality=[],
            interests=[],
            proactive_enabled=char.proactive_enabled,
        )
        response = await character_service.create_character(  # type: ignore[attr-defined]
            request, user_id=op_ids[char.operator_key],
        )
        if not char.proactive_enabled:
            proactive_disabled += 1
        if char.freeze:
            await character_repository.set_frozen(  # type: ignore[attr-defined]
                response.id,
                frozen=True,
                now=resolved_now,
                reason=FREEZE_REASON_MANUAL,
            )
            frozen += 1

    return SeedSummary(
        operators=len(plan.operators),
        characters=len(plan.characters),
        frozen=frozen,
        proactive_disabled=proactive_disabled,
        timezones=plan.timezone_distribution(),
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _resolve_database_url(args: argparse.Namespace) -> str | None:
    if args.database_url:
        return args.database_url
    if not args.i_know_this_is_a_soak_environment:
        print(
            "refusing to seed against the ambient-environment DATABASE_URL "
            "without an explicit opt-in; pass --database-url or "
            "--i-know-this-is-a-soak-environment to target a soak database.",
            file=sys.stderr,
        )
        return None
    env_url = os.getenv("DATABASE_URL", os.getenv("KOKORO_DATABASE_URL", ""))
    if not env_url:
        print("no DATABASE_URL / KOKORO_DATABASE_URL set.", file=sys.stderr)
        return None
    return env_url


async def _run(args: argparse.Namespace) -> int:
    from kokoro_link.bootstrap.container import build_container
    from kokoro_link.bootstrap.settings import AppSettings
    from kokoro_link.domain.entities.operator_profile import OperatorProfile

    database_url = _resolve_database_url(args)
    if database_url is None:
        return 2

    plan = build_seed_plan(
        prefix=args.prefix,
        operators=args.operators,
        characters_per=args.characters_per,
        timezones=tuple(args.timezones),
        freeze_fraction=args.freeze_fraction,
        proactive_disabled_fraction=args.proactive_disabled_fraction,
        seed=args.seed,
    )

    # Point the container at the soak DB. build_container reads DATABASE_URL.
    os.environ["DATABASE_URL"] = database_url
    settings = AppSettings.from_env()
    container = build_container(settings)
    auth_service = getattr(container, "auth_service", None)
    operator_repo = getattr(container, "operator_profile_repository", None)
    if auth_service is None or operator_repo is None:
        print(
            "container has no auth_service / operator repository — is "
            "DATABASE_URL a real PostgreSQL URL?",
            file=sys.stderr,
        )
        return 2

    # Idempotency guard: bail cleanly if this prefix was already seeded.
    existing = await operator_repo.get_by_email(plan.operators[0].email)
    if existing is not None:
        print(
            f"operator {plan.operators[0].email} already exists — this prefix "
            "looks already-seeded. Use a fresh database or a different "
            "--prefix.",
            file=sys.stderr,
        )
        return 3

    # Synthetic in-memory admin actor: create_user only reads actor.is_admin,
    # so we do not depend on the deployment's setup state.
    admin_actor = OperatorProfile(
        id="soak-seeder", display_name="soak seeder", is_admin=True,
    )

    async def _create_operator(
        *, email: str, display_name: str, timezone_id: str, password: str,
    ) -> str:
        # Soak operators are created as admins so the interaction driver can
        # flip freeze state through the real admin route with each operator's
        # own token — a soak-only simplification (an isolated soak DB has no
        # privilege boundary to protect).
        user = await auth_service.create_user(
            actor=admin_actor,
            email=email,
            password=password,
            display_name=display_name,
            is_admin=True,
            timezone_id=timezone_id,
        )
        return user.id

    try:
        summary = await seed_population(
            plan,
            create_operator=_create_operator,
            character_service=container.character_service,
            character_repository=container.character_repository,
            password=soak_password(args.prefix, args.seed),
        )
    finally:
        engine = getattr(container, "db_engine", None)
        if engine is not None:
            await engine.dispose()

    print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seed a deterministic synthetic soak population (operators, "
            "characters, freeze + proactive-disabled fractions) through the "
            "real container services."
        ),
    )
    parser.add_argument("--operators", type=int, default=4)
    parser.add_argument("--characters-per", type=int, default=15)
    parser.add_argument(
        "--timezones",
        type=_csv,
        default=list(_DEFAULT_TIMEZONES),
        help="Comma-separated IANA timezones assigned round-robin to operators.",
    )
    parser.add_argument("--freeze-fraction", type=float, default=0.15)
    parser.add_argument(
        "--proactive-disabled-fraction", type=float, default=0.2,
    )
    parser.add_argument("--seed", type=int, default=20260720)
    parser.add_argument("--prefix", default=_DEFAULT_PREFIX)
    parser.add_argument(
        "--database-url", default="",
        help="Soak DATABASE_URL. Explicitly passing it is the prod-safety guard.",
    )
    parser.add_argument(
        "--i-know-this-is-a-soak-environment", action="store_true",
        help=(
            "Opt-in to read DATABASE_URL from the environment when "
            "--database-url is omitted."
        ),
    )
    return parser


def _csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
