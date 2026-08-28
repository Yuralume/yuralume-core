"""Post-turn parser extracts message_promises with strict tolerance.

Also pins the extraction contract in ``post_turn/processor``: the field
covers every commitment that leaves the user *waiting on the character to
speak first*, not just 「叫我起床」-style errands. Two shapes were silently
falling through before (self-host report 2026-08-12):

* **rendezvous** — 「19:30 一起上線核對任務」 landed only as a
  ``schedule_adjustment``, so no ``SCHEDULED_PROMISE`` row existed and the
  proactive cooldown was free to swallow the whole appointment window.
* **completion** — 「查完資料再找我」 carries no clock time, so it never
  entered the promise mechanism at all and the intention judge skipped it
  forever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.contracts.post_turn import MessagePromise
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.llm_processor import (
    LLMPostTurnProcessor,
    _coerce_iso_datetime,
    _parse_message_promises,
)
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


def test_iso_datetime_with_time_passes() -> None:
    assert _coerce_iso_datetime("2026-05-18T10:00") == "2026-05-18T10:00"
    assert _coerce_iso_datetime(" 2026-05-18T10:00:30 ") == "2026-05-18T10:00"


def test_iso_datetime_rejects_date_only() -> None:
    # No time component → too vague to act on.
    assert _coerce_iso_datetime("2026-05-18") is None


def test_iso_datetime_rejects_malformed() -> None:
    assert _coerce_iso_datetime("tomorrow at 10") is None
    assert _coerce_iso_datetime("") is None
    assert _coerce_iso_datetime(None) is None


def test_parser_keeps_well_formed_promise() -> None:
    out = _parse_message_promises([
        {
            "scheduled_for_iso": "2026-05-18T10:00",
            "intent": "叫使用者起床",
            "source_text": "明天 10 點叫我起床",
        }
    ])
    assert len(out) == 1
    assert isinstance(out[0], MessagePromise)
    assert out[0].intent == "叫使用者起床"
    assert out[0].source_text == "明天 10 點叫我起床"


def test_parser_drops_missing_time() -> None:
    out = _parse_message_promises([
        {"scheduled_for_iso": "", "intent": "叫起床"},
    ])
    assert out == []


def test_parser_drops_missing_intent() -> None:
    out = _parse_message_promises([
        {"scheduled_for_iso": "2026-05-18T10:00", "intent": ""},
    ])
    assert out == []


def test_parser_caps_at_two_per_turn() -> None:
    raw = [
        {"scheduled_for_iso": f"2026-05-18T{h:02d}:00", "intent": f"事 {h}"}
        for h in (9, 10, 11, 12, 13)
    ]
    out = _parse_message_promises(raw)
    assert len(out) == 2


def test_parser_ignores_non_list() -> None:
    assert _parse_message_promises(None) == []
    assert _parse_message_promises({"not": "a list"}) == []


# --------------------------------------------------------------------------
# Extraction contract pinned in the prompt
# --------------------------------------------------------------------------


class _ScriptedModel:
    provider_id = "scripted"

    def __init__(self, response: str) -> None:
        self._response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._response

    async def generate_stream(
        self, prompt: str,
    ) -> AsyncIterator[str]:  # pragma: no cover - unused
        if False:
            yield ""


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="愛玩線上遊戲的大學生",
        personality=["守約"],
        interests=["game"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


async def _rendered_prompt(response: str = '{"memories": []}') -> str:
    model = _ScriptedModel(response)
    processor = LLMPostTurnProcessor(model=model)
    await processor.process(
        character=_character(),
        conversation_id="conv-1",
        user_message="那我們七點半一起上線？",
        assistant_message="好，七點半見。",
        now=datetime(2026, 8, 12, 10, 0, tzinfo=timezone.utc),
    )
    return model.prompts[0]


@pytest.mark.asyncio
async def test_prompt_states_the_waiting_user_criterion() -> None:
    # The old wording keyed on the character literally saying "我會傳訊息
    # 給你"; a 19:30 rendezvous never says that and fell through.
    prompt = await _rendered_prompt()

    assert "message_promises 欄位" in prompt
    assert "在等你先開口" in prompt


@pytest.mark.asyncio
async def test_prompt_covers_two_sided_appointments() -> None:
    prompt = await _rendered_prompt()

    assert "雙向約定" in prompt
    # Not limited to "傳訊息給我" — joint activities count.
    assert "一起上線" in prompt
    assert "赴約" in prompt


@pytest.mark.asyncio
async def test_prompt_covers_completion_promises_without_a_stated_time() -> None:
    prompt = await _rendered_prompt()

    assert "完成型承諾" in prompt
    # The model must estimate an absolute moment rather than skip the entry.
    assert "沒講定時間" in prompt
    assert "仍然要寫一筆" in prompt
    assert "scheduled_for_iso" in prompt


@pytest.mark.asyncio
async def test_prompt_private_activity_exclusion_carves_out_the_new_shapes() -> None:
    # Exclusion (a) used to read "角色自己會做的私人活動 → 只屬於
    # schedule_adjustments", which reads as covering a joint appointment.
    # It must now name the boundary explicitly.
    prompt = await _rendered_prompt()

    assert "既沒有參與、也沒有在等這件事的結果" in prompt
    assert "在等你回報" in prompt


@pytest.mark.asyncio
async def test_prompt_scopes_the_criterion_to_the_three_shapes() -> None:
    # The headline criterion ("對方會不會在等你先開口") is stated
    # unconditionally and 「以下三種情境都算」 reads as illustrative, so a
    # wide reader can extract anything that smells like waiting. It must be
    # limited to (a)/(b)/(c) — a SCHEDULED_PROMISE bypasses quiet hours,
    # daily_limit and cooldown, so an over-wide criterion punches straight
    # through the proactive volume defences.
    prompt = await _rendered_prompt()

    assert "但必須落在下面 (a)(b)(c) 其中一種情境" in prompt
    assert "三種情境以外的一律不寫" in prompt


@pytest.mark.asyncio
async def test_prompt_excludes_self_initiated_offers() -> None:
    # 角色閒聊時自己說「下午去書店，看到有趣的書再跟你分享」，使用者只回
    # 「喔好啊」——沒有要求、也沒有談定，不成立 promise。判準是語意原則
    # （正當性的來源），不是關鍵詞清單。
    prompt = await _rendered_prompt()

    assert "角色單方面自發" in prompt
    assert "客氣附和" in prompt
    assert "不來自角色自己的熱心" in prompt


@pytest.mark.asyncio
async def test_prompt_boundary_note_requires_the_completion_conditions() -> None:
    # The ※ carve-out used to widen 「在等你回報」 past (c)'s own
    # preconditions (使用者要求過 + 角色答應過), contradicting both the
    # exclusion it annotates and (c) itself.
    prompt = await _rendered_prompt()

    assert "成立條件" in prompt
    assert "使用者要求過" in prompt
    assert "角色也答應過" in prompt


@pytest.mark.asyncio
async def test_prompt_keeps_anti_abuse_boundaries() -> None:
    prompt = await _rendered_prompt()

    assert "有空再聊" in prompt
    assert "以後再說" in prompt
    assert "過去時間" in prompt
    assert "最多 1 筆" in prompt


# --------------------------------------------------------------------------
# parse -> persist plumbing for the two newly-covered shapes
# --------------------------------------------------------------------------


def _chat_service(
    pending_repo: InMemoryPendingFollowUpRepository,
) -> ChatService:
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    return ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        pending_follow_up_repository=pending_repo,
    )


async def _extract_and_persist(
    *,
    response: str,
    user_message: str,
    assistant_message: str,
) -> tuple[list, InMemoryPendingFollowUpRepository]:
    processor = LLMPostTurnProcessor(model=_ScriptedModel(response))
    result = await processor.process(
        character=_character(),
        conversation_id="conv-promise",
        user_message=user_message,
        assistant_message=assistant_message,
    )
    pending_repo = InMemoryPendingFollowUpRepository()
    chat = _chat_service(pending_repo)
    await chat._persist_message_promises(
        character_id="char-1",
        conversation_id="conv-promise",
        promises=result.message_promises,
        turn_record_id="turn-1",
    )
    rows = await pending_repo.list_open_for_character("char-1")
    return result.message_promises, rows


def _future_iso(hours: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(hours=hours)
    return moment.strftime("%Y-%m-%dT%H:%M")


@pytest.mark.asyncio
async def test_rendezvous_promise_reaches_pending_follow_ups() -> None:
    # 「19:30 一起上線核對任務」 — the appointment itself is the promise.
    scheduled = _future_iso(3)
    intent = "到約定時間主動找使用者，開始一起核對《森境online》夏祭任務"
    response = (
        '{"memories": [], "schedule_adjustments": [], "message_promises": ['
        f'{{"scheduled_for_iso": "{scheduled}", "intent": "{intent}", '
        '"source_text": "那我們七點半一起上線核對任務"}]}'
    )

    promises, rows = await _extract_and_persist(
        response=response,
        user_message="那我們七點半一起上線核對任務？",
        assistant_message="好，七點半我等你。",
    )

    assert len(promises) == 1
    assert promises[0].intent == intent
    assert len(rows) == 1
    assert rows[0].is_scheduled_promise is True
    assert rows[0].promise_intent == intent


@pytest.mark.asyncio
async def test_completion_promise_reaches_pending_follow_ups() -> None:
    # 「查完資料再找我」 — no clock time was ever spoken, so the extractor
    # estimates one; without it the promise mechanism is never entered.
    scheduled = _future_iso(1.5)
    intent = "查完遊戲改版規則後，回頭向使用者回報查到的結果"
    response = (
        '{"memories": [], "message_promises": ['
        f'{{"scheduled_for_iso": "{scheduled}", "intent": "{intent}", '
        '"source_text": "你查完資料再找我"}]}'
    )

    promises, rows = await _extract_and_persist(
        response=response,
        user_message="你先幫我查一下改版規則，查完再找我。",
        assistant_message="好，我查完就跟你說。",
    )

    assert len(promises) == 1
    assert promises[0].scheduled_for_iso == scheduled
    assert len(rows) == 1
    assert rows[0].is_scheduled_promise is True
    assert rows[0].promise_intent == intent
