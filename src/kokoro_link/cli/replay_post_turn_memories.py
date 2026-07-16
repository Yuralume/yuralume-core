"""Backfill memory extraction for past turns whose post-turn was skipped.

Use case: the early ``/pic``-trigger implementation gated post-turn
extraction (memory + state + schedule/arc adjustments) behind a
``forced_fired`` flag, so any turn that fired the trigger lost its
memory-extraction pass. After the gate was removed (2026-04-25) those
historical turns still have no memories tied to them. This CLI walks
the latest N user/assistant pairs of the latest web conversation and
re-runs the **memory** part of post-turn against them.

Why memory-only:

- ``memories``: the LLM extractor + ``deduplicate(unique, existing)``
  + ``attach_embeddings`` pipeline tolerates being run twice on the
  same turn — duplicates get filtered by content similarity. Net-new
  memories from previously-skipped turns are persisted; already-
  extracted ones are no-ops.
- ``state_suggestion`` / ``schedule_adjustments`` / ``arc_adjustments``:
  these are **delta operations** (``affection_delta=+3``, beat moves,
  activity inserts). Re-running them on turns that already had
  post-turn fire would double-apply, drifting state/schedule/arc into
  an inconsistent place. We deliberately skip them — there's no safe
  way to retroactively know which turns were skipped.

Usage::

    uv run python -m kokoro_link.cli.replay_post_turn_memories \
        --character <character_id> [--last 5]
    uv run python -m kokoro_link.cli.replay_post_turn_memories \
        --character <character_id> --conversation <conv_id> --last 10

Defaults to the latest ``source="web"`` conversation; pass
``--conversation`` to target a specific thread (e.g. a TG / LINE
binding's conversation_id).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.memory.deduplicator import deduplicate
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    seed_legacy_provider_connections,
    sync_provider_connections,
)

_LOGGER = logging.getLogger(__name__)

_DEFAULT_LAST = 5
_DEDUP_POOL = 80


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run memory extraction for the last N turns of a "
            "conversation (memory only — does NOT replay state / "
            "schedule / arc deltas)."
        ),
    )
    parser.add_argument(
        "--character", required=True,
        help="Character id whose conversation we replay against.",
    )
    parser.add_argument(
        "--conversation",
        default=None,
        help=(
            "Specific conversation id (default: latest source='web' "
            "conversation for the character)."
        ),
    )
    parser.add_argument(
        "--last", type=int, default=_DEFAULT_LAST,
        help=(
            f"Replay the last N user/assistant turn pairs "
            f"(default: {_DEFAULT_LAST}). Pairs are (user, assistant) "
            f"tuples; tool-only assistant turns count too."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run extraction but skip persistence — prints would-write counts.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database:
        _LOGGER.error("KOKORO_DATABASE_URL must be set.")
        return 2

    container = build_container(settings)
    await seed_legacy_provider_connections(container, settings)
    await sync_provider_connections(container)
    chat = container.chat_service
    character_repo = chat._character_repository  # noqa: SLF001
    conversation_repo = chat._conversation_repository  # noqa: SLF001
    memory_repo = chat._memory_repository  # noqa: SLF001
    embedder = chat._embedder  # noqa: SLF001
    post_turn = chat._post_turn_processor  # noqa: SLF001

    character = await character_repo.get(args.character)
    if character is None:
        _LOGGER.error("character %s not found", args.character)
        return 2

    if args.conversation:
        conversation = await conversation_repo.get(args.conversation)
        if conversation is None:
            _LOGGER.error("conversation %s not found", args.conversation)
            return 2
    else:
        conversation = await conversation_repo.latest_for_character(
            args.character, source="web",
        )
        if conversation is None:
            _LOGGER.error(
                "no web conversation for character %s", args.character,
            )
            return 2

    pairs = _last_turn_pairs(conversation.messages, args.last)
    if not pairs:
        print(f"No completed turn pairs found in conversation {conversation.id}.")
        return 0

    _LOGGER.info(
        "replaying %d turn pair(s) for character=%s conversation=%s",
        len(pairs), args.character, conversation.id,
    )

    total_extracted = 0
    total_persisted = 0
    for idx, (user_msg, assistant_msg, prior_index) in enumerate(pairs, 1):
        prior_messages = conversation.messages[:prior_index]
        try:
            result = await post_turn.process(
                character=character,
                conversation_id=conversation.id,
                user_message=user_msg.content,
                assistant_message=assistant_msg.content,
                recent_messages=prior_messages,
            )
        except Exception:
            _LOGGER.exception("post-turn processor failed for pair %d", idx)
            continue

        candidates = list(result.memories or [])
        total_extracted += len(candidates)
        if not candidates:
            _LOGGER.info(
                "[pair %d/%d] extractor returned 0 memories",
                idx, len(pairs),
            )
            continue

        # Dedup against existing memories so we don't re-write content
        # that the original (or a prior replay) already captured.
        existing = await memory_repo.query(
            character.id, limit=_DEDUP_POOL,
        )
        unique = deduplicate(candidates, existing)
        if not unique:
            _LOGGER.info(
                "[pair %d/%d] all %d candidates were duplicates of "
                "existing memories",
                idx, len(pairs), len(candidates),
            )
            continue

        if args.dry_run:
            _LOGGER.info(
                "[pair %d/%d] would persist %d new memories (dry-run)",
                idx, len(pairs), len(unique),
            )
            for mem in unique:
                _LOGGER.info(
                    "  → kind=%s salience=%.2f content=%r",
                    mem.kind.value, mem.salience, mem.content[:80],
                )
            continue

        try:
            embedded = await attach_embeddings(unique, embedder)
        except Exception:
            _LOGGER.exception(
                "[pair %d/%d] embedding failed — skipping persist",
                idx, len(pairs),
            )
            continue
        await memory_repo.add_many(embedded)
        total_persisted += len(embedded)
        _LOGGER.info(
            "[pair %d/%d] persisted %d new memories",
            idx, len(pairs), len(embedded),
        )

    print(
        f"Done. Extracted {total_extracted} candidates, "
        f"persisted {total_persisted} new memories "
        f"(dry-run={args.dry_run})."
    )
    return 0


def _last_turn_pairs(
    messages: list[Message], last_n: int,
) -> list[tuple[Message, Message, int]]:
    """Walk ``messages`` newest-last and return up to ``last_n`` complete
    user→assistant pairs. Returns ``(user_msg, assistant_msg,
    prior_index)`` tuples where ``prior_index`` is the slice point
    that gives the historical context the post-turn processor would
    have seen at the moment that turn fired (everything before the
    pair).

    Skips trailing partial turns (a user message with no assistant
    reply yet, or an assistant message with no user before it). This
    prevents replaying half-turns where ``user_message`` /
    ``assistant_message`` would be empty.
    """
    pairs: list[tuple[Message, Message, int]] = []
    i = len(messages) - 1
    while i >= 1 and len(pairs) < last_n:
        msg = messages[i]
        prev = messages[i - 1]
        if (
            msg.role is MessageRole.ASSISTANT
            and prev.role is MessageRole.USER
        ):
            # The historical "prior_messages" the post-turn processor
            # would have seen excludes the user message of this turn
            # — that's passed separately as ``user_message=``. So the
            # slice point is ``i - 1``, not ``i + 1``.
            pairs.append((prev, msg, i - 1))
            i -= 2
        else:
            i -= 1
    pairs.reverse()  # oldest-first so logs read chronologically
    return pairs


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
