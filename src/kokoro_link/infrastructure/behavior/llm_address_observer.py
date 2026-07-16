"""LLM-backed operator address observer (HUMANIZATION_ROADMAP §4.2).

Run from the dream pass tail stage. Reads the operator's most recent
user-side messages and asks the model to characterise the address
style — salutation / formality / response-length preference. Returns a
candidate that ``AddressPreferenceObserverService`` merges with the
prior persisted row.

LLM-first: we do not regex / count honorifics; the model judges. Python
side only enforces output bounds and known band names.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.operator_address_preference import (
    AddressObservationCandidate,
    OperatorAddressObserverPort,
)

_LOGGER = logging.getLogger(__name__)
_MIN_MESSAGES: Final = 4
_VALID_FORMALITY: Final = frozenset({"low", "medium", "high"})
_VALID_LENGTH: Final = frozenset({"short", "medium", "long"})


class NullAddressObserver(OperatorAddressObserverPort):
    """Pass-through observer for tests / fake provider."""

    async def observe(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_user_messages: list[str],
    ) -> AddressObservationCandidate | None:
        return None


class LLMAddressObserver(OperatorAddressObserverPort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def observe(
        self,
        *,
        character_id: str,
        operator_id: str,
        recent_user_messages: list[str],
    ) -> AddressObservationCandidate | None:
        if len(recent_user_messages) < _MIN_MESSAGES:
            return None
        if await self._resolver.is_fake():
            return None
        prompt = _build_prompt(recent_user_messages)
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception(
                "address observer LLM call failed character=%s operator=%s",
                character_id,
                operator_id,
            )
            return None
        return _parse_response(raw)


def _build_prompt(messages: list[str]) -> str:
    joined = "\n".join(f"- {m.strip()[:240]}" for m in messages if m.strip())
    return (
        "你是一位語用觀察員。閱讀使用者最近對某位角色說的訊息，"
        "判斷使用者對該角色的稱呼與說話風格偏好。\n\n"
        f"最近訊息：\n{joined}\n\n"
        "請輸出 JSON 物件，缺少資訊的欄位請留空字串：\n"
        "{\n"
        '  "salutation": "使用者最常稱呼角色的方式（暱稱/你/妳/...）；不確定時留空",\n'
        '  "formality_level": "low|medium|high；不確定時留空",\n'
        '  "response_length_pref": "short|medium|long；不確定時留空",\n'
        '  "evidence_quote": "從上面訊息中複製一句最能支持判斷的原話（不可改寫）"\n'
        "}\n"
        "**禁止**改寫 evidence_quote 內容，必須是訊息中出現過的字句。"
    )


def _parse_response(raw: str) -> AddressObservationCandidate | None:
    if not raw:
        return None
    body = raw.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        _LOGGER.warning("address observer returned unparseable JSON: %s", raw[:200])
        return None
    if not isinstance(data, dict):
        return None
    salutation = str(data.get("salutation") or "").strip()[:64]
    formality_raw = str(data.get("formality_level") or "").strip().lower()
    formality = formality_raw if formality_raw in _VALID_FORMALITY else ""
    length_raw = str(data.get("response_length_pref") or "").strip().lower()
    length = length_raw if length_raw in _VALID_LENGTH else ""
    evidence = str(data.get("evidence_quote") or "").strip()[:240]
    if not salutation and not formality and not length:
        return None
    return AddressObservationCandidate(
        salutation=salutation or None,
        formality_level=formality or None,
        response_length_pref=length or None,
        evidence_quote=evidence,
    )
