"""Replay a recorded LLM turn from the ``turn_records`` table.

Two modes:

* ``--dry-run`` (default): print the recorded prompt, response, latency,
  token usage, and post-turn refs. No LLM call. Used for debugging the
  prompt without spending tokens.
* live: re-send the recorded prompt to a model and print the new
  response next to the original. ``--model`` swaps the model id (any
  string the active provider's ``list_models()`` returns); the original
  recording is **never** mutated and no new turn is recorded.

Usage::

    uv run python -m kokoro_link.cli.replay_turn --turn-id <uuid>
    uv run python -m kokoro_link.cli.replay_turn --turn-id <uuid> --model <id>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.contracts.llm import ChatModelRegistryPort
from kokoro_link.contracts.observability import TurnRecordRepositoryPort
from kokoro_link.domain.entities.turn_record import TurnRecord
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    MetadataCapturingChatModel,
)
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    seed_legacy_provider_connections,
    sync_provider_connections,
)

_LOGGER = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay a recorded turn — print the original or re-run "
            "against a model for comparison. Never mutates the "
            "recorded row."
        ),
    )
    parser.add_argument(
        "--turn-id", required=True,
        help="TurnRecord.id (UUID) to replay.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Print the recorded prompt + response only; do not call any "
            "model. Default behaviour when --model is omitted."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Re-run against this model id (any id the active provider's "
            "``list_models()`` returns). Omit to skip the live call."
        ),
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=(
            "Provider id to resolve the model against. Defaults to the "
            "model_id stored on the recorded turn; if that's empty, "
            "uses the app's default provider."
        ),
    )
    return parser.parse_args(argv)


def _print_record(record: TurnRecord) -> None:
    print("=" * 72)
    print(f"TurnRecord id        : {record.id}")
    print(f"  kind               : {record.kind}")
    print(f"  character_id       : {record.character_id}")
    print(f"  conversation_id    : {record.conversation_id or '(none)'}")
    print(f"  model_id           : {record.model_id or '(none)'}")
    print(f"  latency_ms         : {record.latency_ms}")
    print(f"  prompt_tokens      : {record.prompt_tokens}")
    print(f"  completion_tokens  : {record.completion_tokens}")
    print(f"  created_at         : {record.created_at.isoformat()}")
    if record.error:
        print(f"  error              : {record.error}")
    if record.post_turn_refs:
        print("  post_turn_refs     :")
        print(_indent_json(record.post_turn_refs, "    "))
    if record.response_json is not None:
        print("  response_json      :")
        print(_indent_json(record.response_json, "    "))
    print("-" * 72)
    print("PROMPT")
    print("-" * 72)
    print(record.prompt_assembled or "(no prompt recorded — gate-blocked turn)")
    print("-" * 72)
    print("RESPONSE")
    print("-" * 72)
    print(record.response_text or "(no response recorded)")
    print("=" * 72)


def _indent_json(value: Any, indent: str) -> str:
    return "\n".join(
        indent + line for line in json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    )


async def _live_rerun(
    *,
    record: TurnRecord,
    model_registry: ChatModelRegistryPort,
    provider_id: str | None,
    model_id: str | None,
) -> None:
    if not record.prompt_assembled:
        print("Cannot live-rerun — no prompt was recorded (gate-blocked turn).")
        return
    resolved_provider = provider_id or record.model_id or ""
    if not resolved_provider:
        # Use default — registry list_ids first entry is typically the default.
        ids = model_registry.list_ids()
        if not ids:
            print("No providers registered; cannot live-rerun.")
            return
        resolved_provider = ids[0]
    try:
        inner = model_registry.resolve(resolved_provider)
    except KeyError:
        print(f"Provider {resolved_provider!r} not found.")
        return
    wrapper = MetadataCapturingChatModel(inner)
    print()
    print(f"Live re-run via provider={resolved_provider} model={model_id or '(default)'}")
    captured = await wrapper.generate_capturing(
        record.prompt_assembled, model=model_id,
    )
    print("-" * 72)
    print("LIVE RESPONSE")
    print("-" * 72)
    print(captured.text)
    print("-" * 72)
    print(
        f"latency_ms={captured.metadata.latency_ms} "
        f"model_id={captured.metadata.model_id} "
        f"prompt_tokens~{captured.metadata.prompt_tokens} "
        f"completion_tokens~{captured.metadata.completion_tokens}",
    )
    if captured.metadata.error:
        print(f"error: {captured.metadata.error}")


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    container = build_container(settings)
    await seed_legacy_provider_connections(container, settings)
    await sync_provider_connections(container)
    repo: TurnRecordRepositoryPort | None = container.turn_record_repository
    if repo is None:
        print("Turn record repository not wired in this container.")
        return 2
    record = await repo.get(args.turn_id)
    if record is None:
        print(f"No turn_record found with id={args.turn_id!r}.")
        return 1
    _print_record(record)
    if args.model is not None and not args.dry_run:
        await _live_rerun(
            record=record,
            model_registry=container.model_registry,
            provider_id=args.provider,
            model_id=args.model,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
