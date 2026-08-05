"""Conversation message pagination (plan IV, ticket IV10).

The file is deliberately in two halves.

**Characterization** pins what ``GET /characters/{id}/conversations/latest``
already did before pagination existed — which conversation counts as "latest",
how its messages are ordered, and the wire shape the panel consumes. That route
carried an ownership parametrization and nothing else, so none of it was
protected; truncating the message layer is exactly the kind of change that can
quietly pick a different thread or reverse an order. These assertions must keep
passing verbatim.

**Pagination** covers the new behaviour: the feed's keyset shape (``limit`` +
``before`` in, ``has_more`` / ``next_before`` out) with ``MessageRow.position``
as the cursor.
"""

from __future__ import annotations

from inspect import signature

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.api.dependencies import (
    ensure_owned_character_id,
    get_container,
)
from kokoro_link.api.routes.chat import get_latest_conversation
from kokoro_link.api.routes.chat import router as chat_router
from kokoro_link.application.dto.chat import (
    DEFAULT_CONVERSATION_PAGE_SIZE,
    MAX_CONVERSATION_PAGE_SIZE,
    ConversationResponse,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    ConversationRow,
    MessageRow,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
    SAConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# SQLite SA fixture — only the conversation / message tables (no pgvector).
# Same shape as ``tests/unit/test_conversation_append_cas.py``.
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def sa_repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: ConversationRow.__table__.create(sync_conn),
        )
        await conn.run_sync(
            lambda sync_conn: MessageRow.__table__.create(sync_conn),
        )
    try:
        yield SAConversationRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


def _thread(
    character_id: str, *, count: int, source: str = "web", prefix: str = "m",
) -> Conversation:
    """A conversation with ``count`` alternating messages, ``m0`` … ``m{n-1}``.

    Content doubles as the position label, so an ordering assertion reads as
    the sequence it is checking.
    """
    conversation = Conversation.start(character_id=character_id, source=source)
    for i in range(count):
        conversation = conversation.append(Message(
            role=MessageRole.USER if i % 2 == 0 else MessageRole.ASSISTANT,
            content=f"{prefix}{i}",
        ))
    return conversation


def _contents(page) -> list[str]:
    return [m.content for m in page.messages]


# =========================================================================== #
# Characterization — behaviour that existed before IV10 and must not move
# =========================================================================== #
async def test_characterize_latest_is_the_conversation_with_newest_activity(
    sa_repo: SAConversationRepository,
) -> None:
    """Not insertion order, not creation order: most recent message wins."""
    older = _thread("char-A", count=1, prefix="old")
    await sa_repo.save(older)
    newer = _thread("char-A", count=2, prefix="new")
    await sa_repo.save(newer)
    # Saved last, but belongs to somebody else.
    await sa_repo.save(_thread("char-B", count=3, prefix="other"))

    latest = await sa_repo.latest_for_character("char-A")

    assert latest is not None
    assert latest.id == newer.id


async def test_characterize_latest_filters_to_web_source_by_default(
    sa_repo: SAConversationRepository,
) -> None:
    """A LINE / Telegram thread must never surface in the browser panel."""
    await sa_repo.save(_thread("char-A", count=2, source="line", prefix="line"))
    web = _thread("char-A", count=1, source="web", prefix="web")
    await sa_repo.save(web)
    # LINE activity lands *after* the web thread — recency alone would pick it.
    line_later = _thread("char-A", count=1, source="line", prefix="later")
    await sa_repo.save(line_later)

    assert (await sa_repo.latest_for_character("char-A")).id == web.id
    assert (
        await sa_repo.latest_for_character("char-A", source=None)
    ).id == line_later.id


async def test_characterize_messages_come_back_oldest_first(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=5))

    latest = await sa_repo.latest_for_character("char-A")

    assert [m.content for m in latest.messages] == ["m0", "m1", "m2", "m3", "m4"]
    assert [m.role for m in latest.messages][:2] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]


async def test_characterize_no_conversation_reads_as_none(
    sa_repo: SAConversationRepository,
) -> None:
    assert await sa_repo.latest_for_character("nobody") is None


async def test_characterize_wire_shape_of_a_whole_thread() -> None:
    """The response fields the chat panel binds to, and the message row shape.

    ``kind`` rides along per message (SC1-A) and defaults to ``chat`` for every
    message stored before scenes existed — a thread that loses it reformats
    ordinary dialogue as narration.
    """
    conversation = Conversation.start(character_id="char-A")
    conversation = conversation.append(
        Message(role=MessageRole.USER, content="hi"),
    )
    conversation = conversation.append(Message(
        role=MessageRole.ASSISTANT,
        content="the room settles",
        kind=MessageKind.SCENE_NARRATION,
    ))

    payload = ConversationResponse.from_domain(conversation).model_dump()

    assert payload["id"] == conversation.id
    assert payload["character_id"] == "char-A"
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]
    assert [m["content"] for m in payload["messages"]] == [
        "hi", "the room settles",
    ]
    assert [m["kind"] for m in payload["messages"]] == [
        "chat", "scene_narration",
    ]


async def test_characterize_full_thread_read_is_still_untruncated(
    sa_repo: SAConversationRepository,
) -> None:
    """``latest_for_character`` feeds prompt building, proactive delivery and
    story scenes — all of which need the whole thread, and one of which
    (``save``) would delete whatever a truncated snapshot left out."""
    await sa_repo.save(_thread("char-A", count=DEFAULT_CONVERSATION_PAGE_SIZE * 3))

    latest = await sa_repo.latest_for_character("char-A")

    assert len(latest.messages) == DEFAULT_CONVERSATION_PAGE_SIZE * 3
    assert latest.loaded_message_count == DEFAULT_CONVERSATION_PAGE_SIZE * 3


# =========================================================================== #
# Pagination — the new keyset read
# =========================================================================== #
async def test_page_returns_only_the_newest_messages(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=120))

    page = await sa_repo.latest_page_for_character("char-A", limit=50)

    assert _contents(page) == [f"m{i}" for i in range(70, 120)]
    assert page.has_more is True
    # Cursor is the position of the page's OLDEST message, exclusive.
    assert page.next_before == 70


async def test_page_before_cursor_walks_backwards_without_gap_or_overlap(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=25))

    first = await sa_repo.latest_page_for_character("char-A", limit=10)
    second = await sa_repo.latest_page_for_character(
        "char-A", limit=10, before_position=first.next_before,
    )
    third = await sa_repo.latest_page_for_character(
        "char-A", limit=10, before_position=second.next_before,
    )

    assert _contents(first) == [f"m{i}" for i in range(15, 25)]
    assert _contents(second) == [f"m{i}" for i in range(5, 15)]
    assert _contents(third) == [f"m{i}" for i in range(0, 5)]
    assert third.has_more is False
    assert third.next_before is None
    # Reassembled, the pages are the thread — nothing dropped, nothing doubled.
    assert (
        _contents(third) + _contents(second) + _contents(first)
        == [f"m{i}" for i in range(25)]
    )


async def test_page_exactly_at_limit_reports_no_more(
    sa_repo: SAConversationRepository,
) -> None:
    """The off-by-one that a ``len(page) >= limit`` test gets wrong: a thread
    of exactly one page has nothing older, and claiming otherwise makes the
    panel fetch an empty page on every scroll to the top."""
    await sa_repo.save(_thread("char-A", count=10))

    page = await sa_repo.latest_page_for_character("char-A", limit=10)

    assert len(page.messages) == 10
    assert page.has_more is False
    assert page.next_before is None


async def test_page_selects_the_same_conversation_as_the_full_read(
    sa_repo: SAConversationRepository,
) -> None:
    """The red line: pagination truncates the message layer and nothing else."""
    await sa_repo.save(_thread("char-A", count=3, prefix="old"))
    newer = _thread("char-A", count=80, prefix="new")
    await sa_repo.save(newer)
    await sa_repo.save(_thread("char-A", count=99, source="line", prefix="line"))

    full = await sa_repo.latest_for_character("char-A")
    page = await sa_repo.latest_page_for_character("char-A", limit=5)

    assert page.conversation_id == full.id == newer.id
    assert page.character_id == "char-A"
    # …and the page really is that conversation's tail.
    assert _contents(page) == [m.content for m in full.messages][-5:]


async def test_page_is_none_only_when_there_is_no_conversation(
    sa_repo: SAConversationRepository,
) -> None:
    assert await sa_repo.latest_page_for_character("nobody", limit=10) is None

    empty = Conversation.start(character_id="char-A")
    await sa_repo.save(empty)

    page = await sa_repo.latest_page_for_character("char-A", limit=10)
    assert page is not None
    assert page.messages == ()
    assert page.has_more is False


async def test_page_before_the_first_message_is_empty_not_an_error(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=4))

    page = await sa_repo.latest_page_for_character(
        "char-A", limit=10, before_position=0,
    )

    assert page.messages == ()
    assert page.has_more is False
    assert page.next_before is None


async def test_page_keeps_the_source_filter(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=5, source="line", prefix="line"))

    assert await sa_repo.latest_page_for_character("char-A", limit=5) is None

    page = await sa_repo.latest_page_for_character(
        "char-A", limit=5, source=None,
    )
    assert _contents(page) == [f"line{i}" for i in range(5)]


async def test_in_memory_twin_matches_the_sa_repository(
    sa_repo: SAConversationRepository,
) -> None:
    """Most unit tests run against the in-memory repo; a page shape that only
    holds in SQL is a trap for every one of them."""
    memory_repo = InMemoryConversationRepository()
    thread = _thread("char-A", count=37)
    await sa_repo.save(thread)
    await memory_repo.save(thread)

    for kwargs in (
        {"limit": 10},
        {"limit": 10, "before_position": 27},
        {"limit": 10, "before_position": 7},
        {"limit": 100},
        {"limit": 10, "before_position": 0},
    ):
        sa_page = await sa_repo.latest_page_for_character("char-A", **kwargs)
        memory_page = await memory_repo.latest_page_for_character(
            "char-A", **kwargs,
        )
        assert _contents(sa_page) == _contents(memory_page), kwargs
        assert sa_page.has_more == memory_page.has_more, kwargs
        assert sa_page.next_before == memory_page.next_before, kwargs


async def test_page_response_carries_the_cursor_onto_the_wire(
    sa_repo: SAConversationRepository,
) -> None:
    await sa_repo.save(_thread("char-A", count=12))

    page = await sa_repo.latest_page_for_character("char-A", limit=5)
    payload = ConversationResponse.from_page(page).model_dump()

    assert payload["character_id"] == "char-A"
    assert [m["content"] for m in payload["messages"]] == [
        f"m{i}" for i in range(7, 12)
    ]
    assert payload["has_more"] is True
    assert payload["next_before"] == 7


async def test_whole_thread_response_declares_nothing_older() -> None:
    """A client that pages must not be told to keep paging by the one code
    path that genuinely hands over everything."""
    payload = ConversationResponse.from_domain(
        _thread("char-A", count=3),
    ).model_dump()

    assert payload["has_more"] is False
    assert payload["next_before"] is None


async def test_page_size_defaults_are_sane() -> None:
    assert 0 < DEFAULT_CONVERSATION_PAGE_SIZE <= MAX_CONVERSATION_PAGE_SIZE


# =========================================================================== #
# Route seam
# =========================================================================== #
class _RecordingChatService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def get_latest_conversation(self, character_id: str, **kwargs):
        self.calls.append((character_id, kwargs))
        return None


async def test_route_forwards_the_keyset_parameters() -> None:
    chat_service = _RecordingChatService()

    class _Container:
        pass

    container = _Container()
    container.chat_service = chat_service  # type: ignore[attr-defined]

    await get_latest_conversation(
        "char-A",
        limit=25,
        before=140,
        container=container,  # type: ignore[arg-type]
        _owned_character_id="char-A",
    )

    assert chat_service.calls == [
        ("char-A", {"limit": 25, "before_position": 140}),
    ]


def _client(chat_service) -> TestClient:
    """The chat router alone, with auth/ownership stubbed out, so the query
    contract is exercised over real HTTP rather than a direct call (which
    bypasses FastAPI's parsing and every bound entirely)."""
    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1")

    class _Container:
        pass

    container = _Container()
    container.chat_service = chat_service  # type: ignore[attr-defined]
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[ensure_owned_character_id] = lambda: "char-A"
    return TestClient(app)


async def test_route_without_pagination_preserves_legacy_contract() -> None:
    chat_service = _RecordingChatService()

    response = _client(chat_service).get(
        "/api/v1/characters/char-A/conversations/latest",
    )

    assert response.status_code == 200
    assert chat_service.calls == [(
        "char-A", {"limit": None, "before_position": None},
    )]


async def test_route_over_http_parses_the_cursor_as_an_integer() -> None:
    chat_service = _RecordingChatService()

    response = _client(chat_service).get(
        "/api/v1/characters/char-A/conversations/latest?limit=10&before=0",
    )

    assert response.status_code == 200
    assert chat_service.calls == [
        ("char-A", {"limit": 10, "before_position": 0}),
    ]


async def test_route_over_http_refuses_to_serve_the_whole_thread_again() -> None:
    chat_service = _RecordingChatService()
    client = _client(chat_service)

    assert client.get(
        "/api/v1/characters/char-A/conversations/latest"
        f"?limit={MAX_CONVERSATION_PAGE_SIZE + 1}",
    ).status_code == 422
    assert client.get(
        "/api/v1/characters/char-A/conversations/latest?limit=0",
    ).status_code == 422
    assert client.get(
        "/api/v1/characters/char-A/conversations/latest?before=-1",
    ).status_code == 422
    assert chat_service.calls == []


async def test_route_default_and_clamp_come_from_the_shared_constants() -> None:
    """The panel and the server must agree on what "a page" is; a route that
    quietly defaults to something else is how 316 messages came back in the
    first place."""
    limit_param = signature(get_latest_conversation).parameters["limit"].default
    bounds = {
        type(c).__name__: getattr(c, type(c).__name__.lower())
        for c in limit_param.metadata
    }

    assert limit_param.default is None
    assert bounds == {"Ge": 1, "Le": MAX_CONVERSATION_PAGE_SIZE}
