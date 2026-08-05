"""Showcase pipeline — the red lines that keep a private feed private.

Four families, and the first two are the reason this file exists:

1. **Mechanical filter.** Structural unfitness is decided by provenance,
   not by reading the text: a ``silence`` post is about the owner's
   cadence, and a ``schedule`` post inherits the privacy of the block it
   narrates. Unresolvable provenance fails closed.
2. **Tenant scoping.** A character id is not authority. Pointing the
   pipeline at someone else's character would publish a paying player's
   private feed on the public front page, so every entry point resolves
   the tenant first and a mismatch is indistinguishable from "not found".
3. **Snapshot schema.** Field names are the contract with the portal, and
   a post missing any locale is dropped rather than half-shipped.
4. **Advisory review.** The reviewer can flag and propose a
   de-identifying rewrite; it can never approve, and every failure path
   lands on ``needs_manual_review`` rather than on a clean bill of health.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.showcase.filters import (
    REASON_SCHEDULE_NON_PUBLIC,
    REASON_SCHEDULE_UNRESOLVED,
    PublicActivity,
    ShowcaseCandidate,
    mechanical_filter,
    select_public_activities,
)
from kokoro_link.application.services.showcase.review import (
    VERDICT_FLAG,
    VERDICT_NEEDS_MANUAL_REVIEW,
    VERDICT_PASS,
    ShowcaseReviewer,
    review_candidates,
)
from kokoro_link.application.services.showcase.service import (
    ShowcaseNotFound,
    ShowcaseService,
    TranslationRequest,
    post_source_hash,
)
from kokoro_link.application.services.showcase.snapshot import (
    CharacterCard,
    SnapshotError,
    SnapshotPost,
    build_snapshot,
)
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScenePrivacy,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource

NOW = datetime(2026, 8, 4, 22, 10, tzinfo=timezone.utc)
TENANT = "tenant-showcase"


# --------------------------------------------------------------------------- #
# doubles
# --------------------------------------------------------------------------- #


class ScriptedModel:
    """Chat model double. ``answers`` is consumed in order; an entry that
    is an exception is raised instead of returned."""

    def __init__(self, answers):  # noqa: ANN001
        self._answers = list(answers)
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.prompts.append(prompt)
        if not self._answers:
            raise RuntimeError("scripted model ran out of answers")
        answer = self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class ScriptedProvider:
    """Minimal ``ActiveLLMProviderPort`` double.

    ``per_feature`` lets a test give the reviewer and the translator their
    own scripts, which is how the routing split (two feature keys) is
    asserted at all.
    """

    def __init__(self, per_feature):  # noqa: ANN001
        self._per_feature = per_feature
        self.feature_keys: list[str] = []

    async def resolve(self, feature_key, **kwargs):  # noqa: ANN001, ANN003
        self.feature_keys.append(feature_key)
        return self._per_feature[feature_key]

    async def resolve_model_id(self, feature_key, **kwargs):  # noqa: ANN001, ANN003
        return None

    async def is_fake(self, feature_key, **kwargs) -> bool:  # noqa: ANN001, ANN003
        return False


class FakeCharacterService:
    def __init__(self, characters):  # noqa: ANN001
        self._characters = {character.id: character for character in characters}

    async def get_character_entity(self, character_id, *, user_id=None):  # noqa: ANN001
        character = self._characters.get(character_id)
        if character is None:
            return None
        if user_id is not None and character.user_id != user_id:
            return None
        return character

    async def list_characters(self, *, user_id=None):  # noqa: ANN001
        return [
            character
            for character in self._characters.values()
            if user_id is None or character.user_id == user_id
        ]


class FakeOperatorRepository:
    def __init__(self, by_tenant):  # noqa: ANN001
        self._by_tenant = by_tenant

    async def list_by_cloud_tenant_id(self, cloud_tenant_id):  # noqa: ANN001
        return [
            replace(
                OperatorProfile.default(),
                id=operator_id,
                cloud_tenant_id=cloud_tenant_id,
            )
            for operator_id in self._by_tenant.get(cloud_tenant_id, ())
        ]


class FakeFeedPostRepository:
    def __init__(self, posts):  # noqa: ANN001
        self._posts = list(posts)

    async def list_for_character(self, character_id, *, limit=60):  # noqa: ANN001
        return [
            post for post in self._posts if post.character_id == character_id
        ][:limit]


def make_character(
    character_id: str = "char-1", *, user_id: str = "op-1",
) -> Character:
    character = Character.create(
        name="米菈",
        summary="住在海邊的插畫家",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        image_urls=("/v1/public/characters/mira.png",),
    )
    return replace(character, id=character_id)


def make_post(
    post_id: str,
    *,
    text: str = "今天的天空很藍，我在窗邊坐了很久。",
    source: FeedSource | None = None,
    kind: FeedKind | None = None,
    image_url: str | None = None,
    created_at: datetime | None = None,
    character_id: str = "char-1",
) -> FeedPost:
    return FeedPost.create(
        id=post_id,
        character_id=character_id,
        kind=kind or FeedKind.DAILY,
        content_text=text,
        # Default provenance is deliberately NOT ``schedule``: a schedule
        # post is only publishable if the activity behind it is public, so
        # it needs an activity index to be judged at all (see the
        # "schedule provenance" tests). ``memory`` carries no such tie.
        source=source or FeedSource.memory(f"mem-{post_id}"),
        image_url=image_url,
        image_prompt="masterpiece, 1girl, window light",
        video_prompt="slow drift, cinematic",
        created_at=created_at or NOW,
    )


def passthrough_translator(prefixes: dict[str, str] | None = None):  # noqa: ANN201
    marks = prefixes or {"en": "[en] ", "ja": "[ja] "}

    def _translate(text: str, locale: str) -> str | None:
        return f"{marks.get(locale, '')}{text}"

    return _translate


def build_service(
    *,
    characters=None,  # noqa: ANN001
    posts=(),  # noqa: ANN001
    provider=None,  # noqa: ANN001
    tenants=None,  # noqa: ANN001
) -> ShowcaseService:
    roster = characters if characters is not None else [make_character()]
    return ShowcaseService(
        character_service=FakeCharacterService(roster),
        operator_profile_repository=FakeOperatorRepository(
            tenants if tenants is not None else {TENANT: ("op-1",)},
        ),
        feed_post_repository=FakeFeedPostRepository(posts),
        active_llm_provider=provider,
    )


# --------------------------------------------------------------------------- #
# 1. mechanical filter
# --------------------------------------------------------------------------- #


def test_filter_drops_silence_posts_and_keeps_the_rest():
    outcome = mechanical_filter(
        [
            make_post("p1"),
            make_post("p2", source=FeedSource.silence()),
            make_post("p3", source=FeedSource.beat("beat-9")),
        ],
    )

    assert [c.id for c in outcome.candidates] == ["p1", "p3"]
    assert outcome.excluded == {"source:silence": 1}
    assert outcome.excluded_total == 1


def test_filter_never_carries_internal_fields_out():
    outcome = mechanical_filter([make_post("p1", image_url="/v1/public/feed/a.png")])

    candidate = outcome.candidates[0]
    exported = {name: getattr(candidate, name) for name in ShowcaseCandidate.__slots__}
    assert set(exported) == {"id", "kind", "created_at", "text", "image_url"}
    # The prompts and the provenance ref exist on the source row and have
    # nowhere to land on the candidate — this is a whitelist, not a scrub.
    assert not any(
        "prompt" in name or "ref_id" in name or "reaction" in name
        for name in exported
    )


def _activity(
    description: str,
    *,
    hour: int,
    participant_refs=(),  # noqa: ANN001
    scene_privacy: ScenePrivacy | None = None,
) -> ScheduleActivity:
    base = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    return ScheduleActivity.create(
        start_at=base + timedelta(hours=hour),
        end_at=base + timedelta(hours=hour + 1),
        description=description,
        category="社交",
        participant_refs=list(participant_refs),
        scene_privacy=scene_privacy,
    )


def test_strip_drops_operator_and_private_schedule_blocks():
    base = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    schedule = DailySchedule.create(
        character_id="char-1",
        date_=base.date(),
        activities=[
            _activity("做早餐", hour=7),
            _activity(
                "和飼主去吃拉麵",
                hour=12,
                participant_refs=[
                    ParticipantRef(
                        actor_kind="operator",
                        actor_id="default",
                        display_name="飼主",
                        role="operator_confirmed_shared",
                    ),
                ],
            ),
            _activity("泡澡", hour=22, scene_privacy=ScenePrivacy.INTIMATE),
        ],
    )

    now_entry, strip = select_public_activities(
        schedule, now=base + timedelta(hours=7, minutes=30), local_tz=timezone.utc,
    )

    assert [entry.text for entry in strip] == ["做早餐"]
    assert now_entry is not None
    assert now_entry.live is True


def test_now_is_none_when_the_current_block_is_not_public():
    base = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
    schedule = DailySchedule.create(
        character_id="char-1",
        date_=base.date(),
        activities=[
            _activity(
                "和飼主去吃拉麵",
                hour=12,
                participant_refs=[
                    ParticipantRef(
                        actor_kind="operator",
                        actor_id="default",
                        display_name="飼主",
                    ),
                ],
            ),
        ],
    )

    now_entry, strip = select_public_activities(
        schedule, now=base + timedelta(hours=12, minutes=30), local_tz=timezone.utc,
    )

    assert now_entry is None
    assert strip == ()


def test_filter_rejects_a_schedule_post_from_a_block_the_operator_is_in():
    """The strip already refuses this activity; the post about it says more.

    "今天跟他一起去吃拉麵" reads as an ordinary character post — nothing in
    the text tells anyone the owner was there. Only the provenance row
    knows, so the mechanical filter has to answer, not the human queue.
    """
    solo = _activity("一個人做早餐", hour=7)
    with_owner = _activity(
        "和飼主去吃拉麵",
        hour=12,
        participant_refs=[
            ParticipantRef(
                actor_kind="operator",
                actor_id="default",
                display_name="飼主",
                role="operator_confirmed_shared",
            ),
        ],
    )

    outcome = mechanical_filter(
        [
            make_post("p1", source=FeedSource.schedule(solo.id)),
            make_post(
                "p2",
                text="今天跟他一起去吃了拉麵，湯頭很濃。",
                source=FeedSource.schedule(with_owner.id),
            ),
        ],
        activities={solo.id: solo, with_owner.id: with_owner},
    )

    assert [c.id for c in outcome.candidates] == ["p1"]
    assert outcome.excluded == {REASON_SCHEDULE_NON_PUBLIC: 1}


def test_filter_rejects_a_schedule_post_from_a_private_block():
    private = _activity("泡澡", hour=22, scene_privacy=ScenePrivacy.INTIMATE)

    outcome = mechanical_filter(
        [make_post("p1", source=FeedSource.schedule(private.id))],
        activities={private.id: private},
    )

    assert outcome.candidates == ()
    assert outcome.excluded == {REASON_SCHEDULE_NON_PUBLIC: 1}


def test_filter_fails_closed_when_the_source_activity_is_unresolvable():
    """No index (or a pruned day) is "cannot establish", not "fine"."""
    kept = _activity("一個人去圖書館", hour=10)

    without_index = mechanical_filter(
        [make_post("p1", source=FeedSource.schedule(kept.id))],
    )
    stale_ref = mechanical_filter(
        [make_post("p1", source=FeedSource.schedule("act-deleted"))],
        activities={kept.id: kept},
    )

    assert without_index.candidates == ()
    assert without_index.excluded == {REASON_SCHEDULE_UNRESOLVED: 1}
    assert stale_ref.candidates == ()
    assert stale_ref.excluded == {REASON_SCHEDULE_UNRESOLVED: 1}


def test_non_schedule_posts_never_need_an_activity_index():
    """The gate is scoped to schedule provenance — a memory- or beat-sourced
    post has no block behind it and must not be swept up by the fail-closed
    branch."""
    outcome = mechanical_filter(
        [
            make_post("p1", source=FeedSource.memory("mem-1")),
            make_post("p2", source=FeedSource.beat("beat-1")),
            make_post("p3", source=FeedSource.manual()),
        ],
    )

    assert [c.id for c in outcome.candidates] == ["p1", "p2", "p3"]


# --------------------------------------------------------------------------- #
# 2. tenant scoping
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_candidates_are_scoped_to_the_tenant_that_owns_the_character():
    service = build_service(posts=[make_post("p1")])

    bundle = await service.list_candidates(
        tenant_id=TENANT, character_id="char-1",
    )

    assert [c.id for c in bundle.outcome.candidates] == ["p1"]
    assert bundle.posts_scanned == 1


@pytest.mark.asyncio
async def test_another_tenants_character_is_not_found_not_forbidden():
    """A distinct "wrong tenant" answer is an existence oracle over other
    people's characters — and the failure this guard prevents is publishing
    a paying player's private feed on the public front page."""
    service = build_service(
        characters=[make_character("char-9", user_id="op-other")],
        posts=[make_post("p1", character_id="char-9")],
        tenants={TENANT: ("op-1",), "tenant-other": ("op-other",)},
    )

    with pytest.raises(ShowcaseNotFound):
        await service.list_candidates(tenant_id=TENANT, character_id="char-9")


@pytest.mark.asyncio
async def test_blank_tenant_is_refused_rather_than_treated_as_wildcard():
    service = build_service(posts=[make_post("p1")])

    with pytest.raises(Exception):
        await service.list_candidates(tenant_id="  ", character_id="char-1")


@pytest.mark.asyncio
async def test_character_listing_only_returns_the_tenants_own_roster():
    service = build_service(
        characters=[
            make_character("char-1", user_id="op-1"),
            make_character("char-9", user_id="op-other"),
        ],
        tenants={TENANT: ("op-1",), "tenant-other": ("op-other",)},
    )

    summaries = await service.list_characters(tenant_id=TENANT)

    assert [summary.id for summary in summaries] == ["char-1"]


# --------------------------------------------------------------------------- #
# 3. snapshot schema
# --------------------------------------------------------------------------- #


def _card() -> CharacterCard:
    return CharacterCard(
        name="米菈",
        persona="住在海邊的插畫家",
        portrait_url="/v1/public/characters/mira.png",
        days=214,
        memories=1820,
        stories=7,
    )


def _snapshot_post(
    post_id: str = "p1",
    *,
    text=None,  # noqa: ANN001
    image_url: str | None = None,
) -> SnapshotPost:
    return SnapshotPost(
        id=post_id,
        kind="daily",
        created_at="2026-08-04T22:10:00Z",
        text=text
        or {"zh": "今天的天空很藍。", "en": "The sky is blue.", "ja": "空が青い。"},
        image_url=image_url,
    )


def test_snapshot_matches_the_producer_consumer_contract():
    result = build_snapshot(
        card=_card(),
        posts=[_snapshot_post()],
        now_entry=PublicActivity(time="22:10", text="看書", live=True),
        schedule=[PublicActivity(time="07:30", text="做早餐", live=False)],
        base_url="https://play.yuralume.com/",
        locales=("zh", "en", "ja"),
        translate=passthrough_translator(),
        generated_at=NOW,
    )

    document = result.document
    assert document["generated_at"] == "2026-08-04T22:10:00Z"

    character = document["character"]
    assert set(character) == {
        "name", "persona", "avatar_url", "portrait_url", "stats",
    }
    assert character["name"] == {"zh": "米菈", "en": "[en] 米菈", "ja": "[ja] 米菈"}
    assert character["stats"] == {"days": 214, "memories": 1820, "stories": 7}
    # Relative storage URLs become absolute; a trailing slash on the base
    # URL does not produce a double slash.
    assert (
        character["portrait_url"]
        == "https://play.yuralume.com/v1/public/characters/mira.png"
    )
    assert character["avatar_url"] == character["portrait_url"]

    assert document["now"] == {
        "time": "22:10",
        "text": {"zh": "看書", "en": "[en] 看書", "ja": "[ja] 看書"},
    }
    assert document["schedule"] == [
        {
            "time": "07:30",
            "text": {"zh": "做早餐", "en": "[en] 做早餐", "ja": "[ja] 做早餐"},
            "live": False,
        },
    ]

    post = document["posts"][0]
    assert set(post) == {"id", "kind", "created_at", "text"}
    assert post["text"] == {
        "zh": "今天的天空很藍。",
        "en": "The sky is blue.",
        "ja": "空が青い。",
    }


def test_snapshot_absolutises_post_images_and_drops_unusable_ones():
    result = build_snapshot(
        card=_card(),
        posts=[
            _snapshot_post("p1", image_url="/v1/public/feed/char-1/abc.png"),
            _snapshot_post("p2", image_url="feed/relative.png"),
        ],
        now_entry=None,
        schedule=[],
        base_url="https://play.yuralume.com",
        translate=passthrough_translator(),
    )

    posts = {item["id"]: item for item in result.document["posts"]}
    assert (
        posts["p1"]["image_url"]
        == "https://play.yuralume.com/v1/public/feed/char-1/abc.png"
    )
    # An unusable URL costs the image, not the post.
    assert "image_url" not in posts["p2"]
    assert any("無法絕對化" in warning for warning in result.warnings)


def test_post_missing_a_locale_is_skipped_rather_than_half_shipped():
    result = build_snapshot(
        card=_card(),
        posts=[
            _snapshot_post("p1"),
            _snapshot_post("p2", text={"zh": "只有中文。", "en": "Chinese only."}),
        ],
        now_entry=None,
        schedule=[],
        base_url="https://play.yuralume.com",
        translate=passthrough_translator(),
    )

    assert [item["id"] for item in result.document["posts"]] == ["p1"]
    assert result.skipped_posts == ["p2"]
    assert any("缺 ja 譯文" in warning for warning in result.warnings)


def test_ephemeral_strings_fall_back_to_chinese_with_a_warning():
    """Opposite trade to posts: the schema cannot express "this character
    has no name in English", and a missing key breaks the render."""
    result = build_snapshot(
        card=_card(),
        posts=[],
        now_entry=None,
        schedule=[],
        base_url="https://play.yuralume.com",
        translate=lambda text, locale: None,
    )

    assert result.document["character"]["name"]["en"] == "米菈"
    assert any("沿用中文原文" in warning for warning in result.warnings)


def test_snapshot_rejects_a_locale_set_without_the_source_language():
    with pytest.raises(SnapshotError):
        build_snapshot(
            card=_card(),
            posts=[],
            now_entry=None,
            schedule=[],
            base_url="https://play.yuralume.com",
            locales=("en", "ja"),
        )


def test_snapshot_rejects_a_relative_base_url():
    with pytest.raises(SnapshotError):
        build_snapshot(
            card=_card(),
            posts=[],
            now_entry=None,
            schedule=[],
            base_url="/v1/public",
        )


def test_source_hash_changes_with_the_text_it_fingerprints():
    """The control plane stores this instead of the post body and compares
    it again at publish time: an approval refers to the words that were
    read."""
    assert post_source_hash("今天的天空很藍。") == post_source_hash("今天的天空很藍。")
    assert post_source_hash("今天的天空很藍。") != post_source_hash("今天的天空很灰。")


# --------------------------------------------------------------------------- #
# 4. advisory review
# --------------------------------------------------------------------------- #


def _reviewer(answers) -> ShowcaseReviewer:  # noqa: ANN001
    from kokoro_link.application.services.model_resolver import ModelResolver

    provider = ScriptedProvider({"showcase_review": ScriptedModel(answers)})
    return ShowcaseReviewer(
        ModelResolver(provider=provider, feature_key="showcase_review"),
    )


@pytest.mark.asyncio
async def test_review_flag_carries_reasons_and_a_de_identifying_rewrite():
    reviewer = _reviewer(
        [
            '{"verdict": "flag", "reasons": ["第 2 句出現台南（第 1 項）"], '
            '"rewrite": "今天去了南部的一間咖啡店。"}',
        ],
    )

    summary = await review_candidates(
        [
            ShowcaseCandidate(
                id="p1", kind="daily", created_at=NOW, text="今天去了台南的咖啡店。",
            ),
        ],
        reviewer=reviewer,
    )

    review = summary.reviews[0]
    assert review.verdict == VERDICT_FLAG
    assert review.reasons == ("第 2 句出現台南（第 1 項）",)
    assert review.rewrite == "今天去了南部的一間咖啡店。"
    assert summary.flagged == 1


@pytest.mark.asyncio
async def test_a_pass_never_ships_a_rewrite():
    """A "pass" plus an edit is contradictory — keep the verdict, drop the
    edit, rather than letting the owner approve a change nobody asked for."""
    reviewer = _reviewer(['{"verdict": "pass", "reasons": [], "rewrite": "改寫過的文字"}'])

    summary = await review_candidates(
        [ShowcaseCandidate(id="p1", kind="daily", created_at=NOW, text="很普通的一天。")],
        reviewer=reviewer,
    )

    assert summary.reviews[0].verdict == VERDICT_PASS
    assert summary.reviews[0].rewrite is None


@pytest.mark.asyncio
async def test_transport_failure_costs_one_post_not_the_batch():
    reviewer = _reviewer(
        [RuntimeError("upstream 502"), '{"verdict": "pass", "reasons": []}'],
    )

    summary = await review_candidates(
        [
            ShowcaseCandidate(id="p1", kind="daily", created_at=NOW, text="第一篇。"),
            ShowcaseCandidate(id="p2", kind="daily", created_at=NOW, text="第二篇。"),
        ],
        reviewer=reviewer,
    )

    assert summary.reviews[0].verdict == VERDICT_NEEDS_MANUAL_REVIEW
    assert "502" in summary.reviews[0].reasons[0]
    assert summary.reviews[1].verdict == VERDICT_PASS
    assert (summary.failed, summary.passed) == (1, 1)


@pytest.mark.asyncio
async def test_an_unparseable_answer_is_manual_review_not_a_clean_bill():
    reviewer = _reviewer(["I think this post is fine, honestly."])

    summary = await review_candidates(
        [ShowcaseCandidate(id="p1", kind="daily", created_at=NOW, text="一篇貼文。")],
        reviewer=reviewer,
    )

    assert summary.reviews[0].verdict == VERDICT_NEEDS_MANUAL_REVIEW


@pytest.mark.asyncio
async def test_review_only_answers_for_posts_the_filter_kept():
    """Asking about a rejected post gets no verdict: a post the mechanical
    filter refuses has nothing to approve."""
    provider = ScriptedProvider(
        {"showcase_review": ScriptedModel(['{"verdict": "pass", "reasons": []}'])},
    )
    service = build_service(
        posts=[make_post("p1"), make_post("p2", source=FeedSource.silence())],
        provider=provider,
    )

    summary = await service.review_posts(
        tenant_id=TENANT, character_id="char-1", post_ids=["p1", "p2"],
    )

    assert [review.post_id for review in summary.reviews] == ["p1"]
    assert provider.feature_keys == ["showcase_review"]


@pytest.mark.asyncio
async def test_translation_reports_the_locales_it_could_not_produce():
    """The publisher's rule is that a post missing a locale does not ship,
    and it can only apply that rule if failure is visible."""
    provider = ScriptedProvider(
        {
            "showcase_translate": ScriptedModel(
                ['{"translation": "The sky is blue."}', RuntimeError("timeout")],
            ),
        },
    )
    service = build_service(provider=provider)

    results = await service.translate_texts(
        tenant_id=TENANT,
        character_id="char-1",
        items=[TranslationRequest(key="p1", text="今天的天空很藍。")],
    )

    assert results[0].translations == {"en": "The sky is blue."}
    assert results[0].missing == ("ja",)
