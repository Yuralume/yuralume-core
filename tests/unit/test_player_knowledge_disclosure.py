"""KB8 — the three channels that flip ``private`` memories to ``disclosed``.

The ledger's whole value rests on one asymmetry: failing to record a
disclosure costs a repeated introduction, while recording one that never
happened has the character treat an untold fact as common ground — the
2026-08-25 incident. Every test here is written against that asymmetry
rather than against a happy path, so each channel is pinned twice: once
that it flips when it should, and once that it does *not* flip on the
specific failure that channel can actually produce.

* **chat** — the post-turn pass can name any id it likes; only ids from
  the candidate list this turn injected are allowed to flip.
* **feed** — no model at all. A memory-sourced post the player has read
  discloses its memory, once, however many times the read is reported.
* **proactive** — a judge that cannot answer must leave the ledger
  alone, and one that answers about something it was never shown must be
  ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.memory_disclosure_service import (
    MemoryDisclosureService,
    select_private_candidates,
)
from kokoro_link.contracts.player_knowledge_disclosure import (
    DisclosureCandidate,
    DisclosureVerdict,
)
from kokoro_link.contracts.post_turn import PostTurnResult
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.entities.conversation import (
    Conversation, Message, MessageRole,
)
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.memory_item import (
    PLAYER_KNOWLEDGE_DISCLOSED,
    PLAYER_KNOWLEDGE_PRIVATE,
    PLAYER_KNOWLEDGE_SHARED,
    MemoryItem,
)
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.knowledge.llm_disclosure_judge import (
    _parse_verdict,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _parse_disclosed_memory_ids,
    _render_disclosure_section,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

# Marked per-test rather than module-wide: half the cases here are pure
# parser checks with nothing to await, and a blanket asyncio mark makes
# pytest warn on every one of them.
_async = pytest.mark.asyncio

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)


def _memory(
    item_id: str,
    *,
    character_id: str = "char-1",
    content: str = "我一個人去了林道",
    player_knowledge: str = PLAYER_KNOWLEDGE_PRIVATE,
) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        character_id=character_id,
        conversation_id=None,
        kind=MemoryKind.EPISODIC,
        content=content,
        salience=0.6,
        player_knowledge=player_knowledge,
    )


# --------------------------------------------------------------------------- #
# the repository primitive — the only legal transition
# --------------------------------------------------------------------------- #

@_async
async def test_mark_disclosed_only_moves_private_rows() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add_many([
        _memory("m-private"),
        _memory("m-shared", player_knowledge=PLAYER_KNOWLEDGE_SHARED),
        _memory("m-legacy", player_knowledge=""),
    ])

    flipped = await repo.mark_disclosed(
        "char-1", ["m-private", "m-shared", "m-legacy"],
    )

    assert flipped == ("m-private",)
    assert (await repo.get("m-private")).player_knowledge == (
        PLAYER_KNOWLEDGE_DISCLOSED
    )
    # A ``shared`` memory is already common ground and a legacy ``""`` row
    # was never judged; neither has a disclosure to record, and inventing
    # one would be a verdict nobody reached.
    assert (await repo.get("m-shared")).player_knowledge == (
        PLAYER_KNOWLEDGE_SHARED
    )
    assert (await repo.get("m-legacy")).player_knowledge == ""


@_async
async def test_mark_disclosed_is_idempotent_and_character_scoped() -> None:
    repo = InMemoryMemoryRepository()
    await repo.add_many([
        _memory("m-1"),
        _memory("m-other", character_id="char-2"),
    ])

    assert await repo.mark_disclosed("char-1", ["m-1"]) == ("m-1",)
    # Replay reports nothing new — the caller can log a real transition
    # rather than an attempt.
    assert await repo.mark_disclosed("char-1", ["m-1"]) == ()
    # A caller holding one character's ownership cannot reach another's.
    assert await repo.mark_disclosed("char-1", ["m-other"]) == ()
    assert (await repo.get("m-other")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_disclose_survives_a_broken_repository() -> None:
    class _Broken(InMemoryMemoryRepository):
        async def mark_disclosed(self, character_id, item_ids):  # noqa: ANN001
            raise RuntimeError("memory table unreachable")

    service = MemoryDisclosureService(memories=_Broken())
    # Every caller is past the point of delivery; a ledger write that
    # fails must not become an exception the send path has to handle.
    assert await service.disclose(
        character_id="char-1", memory_ids=["m-1"],
    ) == ()


def test_select_private_candidates_drops_everything_already_settled() -> None:
    items = [
        _memory("m-private"),
        _memory("m-shared", player_knowledge=PLAYER_KNOWLEDGE_SHARED),
        _memory("m-told", player_knowledge=PLAYER_KNOWLEDGE_DISCLOSED),
        _memory("m-legacy", player_knowledge=""),
    ]
    assert [item.id for item in select_private_candidates(items)] == [
        "m-private",
    ]


# --------------------------------------------------------------------------- #
# chat — the verdict is bounded by what the turn injected
# --------------------------------------------------------------------------- #

@dataclass
class _SpyPostTurnProcessor:
    """Answers with a fixed verdict and records what it was shown."""

    verdict: list[str] = field(default_factory=list)
    seen: dict = field(default_factory=dict)

    async def process(self, **kwargs):  # noqa: ANN003
        self.seen = kwargs
        return PostTurnResult(disclosed_memory_ids=list(self.verdict))


@dataclass
class _ChatWiring:
    chat: ChatService
    characters: CharacterService
    memories: InMemoryMemoryRepository
    processor: _SpyPostTurnProcessor


def _wire_chat(*, verdict: list[str] | None = None) -> _ChatWiring:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    processor = _SpyPostTurnProcessor(verdict=list(verdict or []))
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=processor,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        extract_in_background=False,
    )
    return _ChatWiring(
        chat=chat,
        characters=CharacterService(character_repository),
        memories=memory_repository,
        processor=processor,
    )


async def _seed_chat(wiring: _ChatWiring) -> Character:
    created = await wiring.characters.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    await wiring.chat._conversation_repository.save(Conversation(  # noqa: SLF001
        id="conv-1", character_id=created.id, messages=[
            Message(
                role=MessageRole.USER, content="今天還好嗎",
                created_at=NOW - timedelta(minutes=2),
            ),
            Message(
                role=MessageRole.ASSISTANT, content="我昨天一個人去了林道",
                created_at=NOW - timedelta(minutes=1),
            ),
        ],
    ))
    return await wiring.chat._character_repository.get(created.id)  # noqa: SLF001


@_async
async def test_chat_flips_only_ids_the_turn_injected() -> None:
    wiring = _wire_chat(verdict=["m-injected", "m-elsewhere"])
    character = await _seed_chat(wiring)
    await wiring.memories.add_many([
        _memory("m-injected", character_id=character.id),
        _memory("m-elsewhere", character_id=character.id),
    ])

    result = await wiring.chat._do_post_turn(  # noqa: SLF001
        character=character,
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="今天還好嗎",
        assistant_text="我昨天一個人去了林道，路上遇到一隻貓",
        prior_messages=[],
        private_memory_ids=("m-injected",),
    )

    # ``m-elsewhere`` is a perfectly real memory of this very character —
    # which is exactly why "does the id exist?" is not the check. It was
    # not in this turn's prompt, so the character cannot have told the
    # player about it here.
    assert result["disclosed_memory_ids"] == ["m-injected"]
    assert (await wiring.memories.get("m-injected")).player_knowledge == (
        PLAYER_KNOWLEDGE_DISCLOSED
    )
    assert (await wiring.memories.get("m-elsewhere")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_chat_shows_the_processor_only_still_private_memories() -> None:
    wiring = _wire_chat()
    character = await _seed_chat(wiring)
    await wiring.memories.add_many([
        _memory("m-private", character_id=character.id),
        _memory(
            "m-told", character_id=character.id,
            player_knowledge=PLAYER_KNOWLEDGE_DISCLOSED,
        ),
    ])

    await wiring.chat._do_post_turn(  # noqa: SLF001
        character=character,
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="今天還好嗎",
        assistant_text="嗯，還可以",
        prior_messages=[],
        # Both went into the prompt; only one still has a transition left.
        private_memory_ids=("m-private", "m-told"),
    )

    shown = wiring.processor.seen.get("disclosure_candidates")
    assert [item.id for item in shown] == ["m-private"]


@_async
async def test_chat_omits_the_section_when_nothing_is_disclosable() -> None:
    wiring = _wire_chat()
    character = await _seed_chat(wiring)

    await wiring.chat._do_post_turn(  # noqa: SLF001
        character=character,
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="今天還好嗎",
        assistant_text="嗯，還可以",
        prior_messages=[],
        private_memory_ids=(),
    )

    # Not "an empty list" — the key is absent, so the template renders no
    # section and the model is never invited to populate one.
    assert "disclosure_candidates" not in wiring.processor.seen


def test_post_turn_parser_allow_lists_the_verdict() -> None:
    known = {"m-1", "m-2"}
    assert _parse_disclosed_memory_ids(
        ["m-1", "m-hallucinated", "m-1", "m-2"], known_ids=known,
    ) == ["m-1", "m-2"]
    # Wrong shape = no verdict = no flip.
    assert _parse_disclosed_memory_ids("m-1", known_ids=known) == []
    assert _parse_disclosed_memory_ids(None, known_ids=known) == []
    # Nothing was offered, so nothing can come back.
    assert _parse_disclosed_memory_ids(["m-1"], known_ids=set()) == []


def test_post_turn_section_prints_ids_and_is_empty_without_candidates() -> None:
    assert _render_disclosure_section(()) == []
    rendered = "\n".join(_render_disclosure_section((_memory("m-1"),)))
    assert "id=m-1" in rendered


# --------------------------------------------------------------------------- #
# feed — structural, idempotent, and only for memory-sourced posts
# --------------------------------------------------------------------------- #

def _post(post_id: str, source: FeedSource, *, character_id: str = "char-1"):
    return FeedPost(
        id=post_id,
        character_id=character_id,
        kind=FeedKind.DAILY,
        content_text="今天的林道很安靜",
        source=source,
        created_at=NOW,
    )


async def _feed_wiring() -> tuple[
    MemoryDisclosureService, InMemoryMemoryRepository, InMemoryFeedPostRepository,
]:
    memories = InMemoryMemoryRepository()
    posts = InMemoryFeedPostRepository()
    return (
        MemoryDisclosureService(memories=memories, feed_posts=posts),
        memories,
        posts,
    )


@_async
async def test_feed_view_discloses_the_source_memory_once() -> None:
    service, memories, posts = await _feed_wiring()
    await memories.add_many([_memory("m-1")])
    await posts.add(_post("p-1", FeedSource.memory("m-1")))

    assert await service.disclose_from_viewed_posts(
        character_id="char-1", post_ids=["p-1"],
    ) == ("m-1",)
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_DISCLOSED
    )
    # The frontend's exposure batcher retries on network failure without
    # knowing whether the first attempt landed, so a replay has to be a
    # no-op rather than a second "transition".
    assert await service.disclose_from_viewed_posts(
        character_id="char-1", post_ids=["p-1", "p-1"],
    ) == ()


@_async
async def test_feed_view_ignores_posts_that_are_not_made_of_a_memory() -> None:
    service, memories, posts = await _feed_wiring()
    await memories.add_many([_memory("m-1")])
    await posts.add(_post("p-beat", FeedSource.beat("beat-9")))
    await posts.add(_post("p-silence", FeedSource.silence()))

    assert await service.disclose_from_viewed_posts(
        character_id="char-1", post_ids=["p-beat", "p-silence", "p-missing"],
    ) == ()
    # A beat post may well allude to the same material, but "alludes to"
    # is an inference; the ledger only records the link the data states.
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_feed_view_cannot_cross_characters() -> None:
    service, memories, posts = await _feed_wiring()
    await memories.add_many([_memory("m-1")])
    await posts.add(_post("p-1", FeedSource.memory("m-1")))

    assert await service.disclose_from_viewed_posts(
        character_id="char-2", post_ids=["p-1"],
    ) == ()
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_feed_reaction_fallback_discloses_from_a_loaded_post() -> None:
    service, memories, posts = await _feed_wiring()
    await memories.add_many([_memory("m-1")])
    post = _post("p-1", FeedSource.memory("m-1"))
    await posts.add(post)

    # The like / comment path already holds the row — reacting is proof
    # of reading even when the exposure report never lands.
    assert await service.disclose_from_post(post) == ("m-1",)
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_DISCLOSED
    )


# --------------------------------------------------------------------------- #
# proactive — a judge that cannot answer changes nothing
# --------------------------------------------------------------------------- #

class _StubJudge:
    def __init__(self, verdict: DisclosureVerdict) -> None:
        self.verdict = verdict
        self.calls = 0

    async def judge(self, *, message_text, candidates, character=None):  # noqa: ANN001
        self.calls += 1
        return self.verdict


class _CrashingJudge:
    async def judge(self, *, message_text, candidates, character=None):  # noqa: ANN001
        raise RuntimeError("upstream down")


def _bare_character() -> Character:
    """The routing handle the flip needs — nothing reaches the prompt."""
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _dispatcher(judge, memories):  # noqa: ANN001
    from kokoro_link.application.services.proactive_dispatcher import (
        ProactiveDispatcher,
    )
    from kokoro_link.infrastructure.proactive.heuristic_gate import (
        HeuristicProactiveGate,
    )
    from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
        InMemoryProactiveAttemptRepository,
    )

    class _NullDecider:
        async def decide(self, context):  # noqa: ANN001
            raise AssertionError("not reached")

    return ProactiveDispatcher(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        account_repository=None,
        binding_repository=None,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(),
        decider=_NullDecider(),
        adapters={},
        memory_repository=memories,
        disclosure_judge=judge,
    )


async def _proactive_case(judge) -> tuple[tuple[str, ...], InMemoryMemoryRepository]:  # noqa: ANN001
    character = _bare_character()
    memories = InMemoryMemoryRepository()
    candidates = (
        _memory("m-1", character_id=character.id),
        _memory("m-2", character_id=character.id),
    )
    await memories.add_many(list(candidates))
    dispatcher = _dispatcher(judge, memories)
    flipped = await dispatcher._flip_disclosed_memories(  # noqa: SLF001
        character=character,
        message_text="我跟你說，我昨天一個人走了林道",
        candidates=candidates,
    )
    return flipped, memories


@_async
async def test_proactive_flips_what_the_judge_says_was_said() -> None:
    flipped, memories = await _proactive_case(
        _StubJudge(DisclosureVerdict.of(("m-1",))),
    )
    assert flipped == ("m-1",)
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_DISCLOSED
    )
    # The push mentioned one of the two recalled memories; the other was
    # context that shaped the tone and never reached the player.
    assert (await memories.get("m-2")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_proactive_judge_failure_flips_nothing() -> None:
    flipped, memories = await _proactive_case(
        _StubJudge(DisclosureVerdict.failed()),
    )
    assert flipped == ()
    for item_id in ("m-1", "m-2"):
        assert (await memories.get(item_id)).player_knowledge == (
            PLAYER_KNOWLEDGE_PRIVATE
        )


@_async
async def test_proactive_judge_crash_flips_nothing() -> None:
    flipped, memories = await _proactive_case(_CrashingJudge())
    assert flipped == ()
    assert (await memories.get("m-1")).player_knowledge == (
        PLAYER_KNOWLEDGE_PRIVATE
    )


@_async
async def test_proactive_ignores_ids_the_judge_was_never_shown() -> None:
    flipped, memories = await _proactive_case(
        _StubJudge(DisclosureVerdict.of(("m-not-offered",))),
    )
    assert flipped == ()


@_async
async def test_proactive_skips_the_call_when_nothing_is_disclosable() -> None:
    memories = InMemoryMemoryRepository()
    judge = _StubJudge(DisclosureVerdict.none())
    dispatcher = _dispatcher(judge, memories)

    assert await dispatcher._flip_disclosed_memories(  # noqa: SLF001
        character=_bare_character(),
        message_text="在忙嗎？",
        candidates=(),
    ) == ()
    # No candidates means there is no question, and a question nobody
    # asked should not be billed.
    assert judge.calls == 0


# --------------------------------------------------------------------------- #
# the judge adapter's parser
# --------------------------------------------------------------------------- #

def test_judge_parser_allow_lists_and_refuses_broken_shapes() -> None:
    allowed = {"m-1", "m-2"}
    assert _parse_verdict(
        '{"disclosed_memory_ids": ["m-1", "m-9"]}', allowed=allowed,
    ) == DisclosureVerdict.of(("m-1",))
    assert _parse_verdict(
        '{"disclosed_memory_ids": []}', allowed=allowed,
    ) == DisclosureVerdict.none()
    # A conclusion is not a payload: a reply cut off mid-array is a judge
    # failure, not a half-answer to act on.
    assert _parse_verdict(
        '{"disclosed_memory_ids": ["m-1", "m-2"', allowed=allowed,
    ).unavailable is True
    # Array-shaped means the model gave several answers it could not
    # choose between.
    assert _parse_verdict(
        '[{"disclosed_memory_ids": ["m-1"]}]', allowed=allowed,
    ).unavailable is True
    # A present-but-wrong-typed field is a failure, not "nothing".
    assert _parse_verdict(
        '{"disclosed_memory_ids": "m-1"}', allowed=allowed,
    ).unavailable is True
    assert _parse_verdict("完全不是 JSON", allowed=allowed).unavailable is True


def test_judge_candidate_carries_only_id_and_content() -> None:
    candidate = DisclosureCandidate(memory_id="m-1", content="我去了林道")
    assert candidate.memory_id == "m-1"
    assert candidate.content == "我去了林道"
