"""Stub draft generator — used when no capable LLM is configured.

Returns a minimal placeholder draft so the UI path remains testable in
dev setups running only the ``fake`` provider. Not meant to produce
interesting output.

LLM-FIRST EXEMPTION: these are the deterministic, model-free stubs
wired when ``active_provider is None`` (self-host without an LLM). The
placeholder field values are hardcoded per shipped locale so the
AI-draft button doesn't prefill an en-US / ja-JP operator's character
form with zh-TW text. This is not keyword-matching business logic; it
is a static placeholder the operator immediately overwrites (or the
real LLM adapter supersedes once a provider is configured). New shipped
languages get a new entry in the per-locale tables below.
"""

from __future__ import annotations

from datetime import date

from kokoro_link.contracts.character_draft import (
    CharacterDraft,
    CharacterDraftGeneratorPort,
    CompanionDraft,
    CompanionDraftGeneratorPort,
    CompanionGenerationContext,
    ImageInput,
)

_FALLBACK_LANGUAGE = "zh-TW"


def _resolve_language(language_tag: str | None, supported: dict[str, object]) -> str:
    """Exact tag → language-subtag family → zh-TW (same resolution rule
    the arc planner's synthetic template pack uses)."""
    tag = (language_tag or "").strip()
    if tag in supported:
        return tag
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    if subtag:
        for known in supported:
            if known.split("-", 1)[0].lower() == subtag:
                return known
    return _FALLBACK_LANGUAGE


# --- character draft placeholder field values, per locale -------------
_CHARACTER_STUB: dict[str, dict[str, object]] = {
    "zh-TW": {
        "name": "新角色",
        "default_summary": "尚未設定的角色。",
        "personality": ["溫柔"],
        "interests": ["閱讀"],
        "speaking_style": "自然親切",
    },
    "en-US": {
        "name": "New Character",
        "default_summary": "A character not yet defined.",
        "personality": ["gentle"],
        "interests": ["reading"],
        "speaking_style": "natural and warm",
    },
    "ja-JP": {
        "name": "新しいキャラクター",
        "default_summary": "まだ設定されていないキャラクター。",
        "personality": ["優しい"],
        "interests": ["読書"],
        "speaking_style": "自然で親しみやすい",
    },
}

# --- companion draft placeholder field values, per locale -------------
_COMPANION_STUB: dict[str, dict[str, object]] = {
    "zh-TW": {
        "name": "室友",
        "role": "室友",
        "brief_profile": "個性互補、生活作息接近的同居人。",
        "personality_sketch": ["隨和"],
        "relationship_snippet": "一起住一陣子，平常會分享一些近況。",
    },
    "en-US": {
        "name": "Roommate",
        "role": "roommate",
        "brief_profile": (
            "A housemate with a complementary personality and a similar "
            "daily rhythm."
        ),
        "personality_sketch": ["easygoing"],
        "relationship_snippet": (
            "They've lived together for a while and usually share little "
            "updates about their days."
        ),
    },
    "ja-JP": {
        "name": "ルームメイト",
        "role": "ルームメイト",
        "brief_profile": "性格が補い合い、生活リズムの近い同居人。",
        "personality_sketch": ["おおらか"],
        "relationship_snippet": (
            "しばらく一緒に暮らしていて、普段は近況を少し共有する間柄。"
        ),
    },
}


class StubCompanionDraftGenerator(CompanionDraftGeneratorPort):
    """Deterministic placeholder used when no capable LLM is wired.

    Returns a single generic companion so the operator can still
    eyeball the end-to-end flow in dev setups running only the
    ``fake`` provider. Real picks happen via the LLM adapter."""

    async def generate(
        self, *, context: CompanionGenerationContext,
    ) -> list[CompanionDraft]:
        language = _resolve_language(
            context.operator_primary_language, _COMPANION_STUB,
        )
        values = _COMPANION_STUB[language]
        return [
            CompanionDraft(
                name=str(values["name"]),
                role=str(values["role"]),
                brief_profile=str(values["brief_profile"]),
                personality_sketch=list(values["personality_sketch"]),  # type: ignore[arg-type]
                relationship_snippet=str(values["relationship_snippet"]),
            ),
        ]


class StubCharacterDraftGenerator(CharacterDraftGeneratorPort):
    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> CharacterDraft:
        _ = operator_id
        language = _resolve_language(
            operator_primary_language, _CHARACTER_STUB,
        )
        values = _CHARACTER_STUB[language]
        hint = (prompt or "").strip()
        return CharacterDraft(
            name=str(values["name"]),
            summary=hint or str(values["default_summary"]),
            personality=list(values["personality"]),  # type: ignore[arg-type]
            interests=list(values["interests"]),  # type: ignore[arg-type]
            speaking_style=str(values["speaking_style"]),
            boundaries=[],
            aspirations=[],
            appearance="",
            date_of_birth=date(2000, 1, 1),
            world_frame="modern",
        )
