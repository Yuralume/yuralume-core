"""LLM-backed persona consolidator (the "dream job" brain).

Runs during quiet hours when there's enough staging to act on. Given
the current confirmed persona, the pending-candidate buffer, and
fields eligible for decay, returns a structured action plan
(:class:`ConsolidationResult`) that the application service applies
inside one transaction.

What the LLM does that pure code can't:

- Decide when two candidates with different values refer to the
  same underlying fact (merge vs supersede).
- Tell whether a Layer 3 candidate truly carries a first-person
  signal (rather than just containing "我" inside a quote).
- Spot cross-field patterns worth inferring at low confidence
  (work overtime + sleep poorly + self-deprecating → maybe work
  anxiety, marked ``dream_inference``).

What pure code does (also enforced here as a guard):

- Layer-specific confidence caps (Layer 3 ≤ 0.9, infer ≤ 0.6).
- Reject malformed actions silently — a bad LLM batch should not
  break the next dream pass.
"""

from __future__ import annotations

import json
import logging

from kokoro_link.application.services.feature_keys import FEATURE_PERSONA_DREAM
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_consolidator import (
    ConsolidationResult,
    DecayAction,
    InferAction,
    MergeAction,
    PersonaConsolidatorPort,
    PromoteAction,
    RejectAction,
    SupersedeAction,
)
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    ProfileField,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_INFER_CONFIDENCE = 0.6
_LAYER_MAX_CONFIDENCE: dict[int, float] = {
    1: 0.95,
    2: 0.95,
    3: 0.90,  # Layer 3 caps lower — emotional inferences shouldn't go absolute.
    5: 0.95,
}
_VALID_LAYERS: frozenset[int] = frozenset({1, 2, 3, 5})
_LAYER_FIELD_KEYS: dict[int, frozenset[str]] = {
    1: frozenset({
        "name", "nickname", "age", "occupation",
        "company_or_school", "residence", "family",
    }),
    2: frozenset({
        "interests", "diet", "routine", "consumption_style",
        "income_band", "relationship_status", "life_goals",
    }),
    3: frozenset({
        "anxieties", "traumas", "secrets", "vulnerabilities",
        "values", "openness_level",
    }),
    5: frozenset({
        "money_borrowed", "help_asked", "vulnerability_shown",
        "family_introduced", "resource_shared", "secret_kept",
    }),
}


class LLMPersonaConsolidator(PersonaConsolidatorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model,
            feature_key=FEATURE_PERSONA_DREAM,
        )

    async def consolidate(
        self,
        *,
        persona: OperatorPersona,
        pending: list[CandidateField],
        decay_candidates: list[ProfileField],
    ) -> ConsolidationResult:
        if await self._resolver.is_fake():
            return ConsolidationResult()
        pending = [cand for cand in pending if not _is_sensitive_candidate(cand)]
        decay_candidates = [
            fld for fld in decay_candidates if not _is_sensitive_field(fld)
        ]
        if not pending and not decay_candidates:
            return ConsolidationResult()
        prompt = _build_prompt(
            persona=persona,
            pending=pending,
            decay_candidates=decay_candidates,
        )
        try:
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("Persona dream LLM call failed")
            return ConsolidationResult()
        candidate_by_id = {
            c.candidate_id: c for c in pending if c.candidate_id
        }
        valid_field_ids = {f.field_id for f in decay_candidates if f.field_id}
        confirmed_by_id = _collect_confirmed_fields(persona)
        return _parse_response(
            raw,
            candidate_by_id=candidate_by_id,
            valid_field_ids=valid_field_ids,
            confirmed_by_id=confirmed_by_id,
        )


def _collect_confirmed_fields(persona: OperatorPersona) -> dict[str, ProfileField]:
    out: dict[str, ProfileField] = {}
    for layer in (1, 2, 3, 5):
        for fld in persona.fields_by_layer(layer).values():
            if fld.field_id and not _is_sensitive_field(fld):
                out[fld.field_id] = fld
    return out


def _build_prompt(
    *,
    persona: OperatorPersona,
    pending: list[CandidateField],
    decay_candidates: list[ProfileField],
) -> str:
    confirmed_lines = ["[現有 confirmed 欄位]"]
    has_any = False
    for layer in (1, 2, 3, 5):
        fields = persona.fields_by_layer(layer)
        for key, fld in fields.items():
            if _is_sensitive_field(fld):
                continue
            has_any = True
            confirmed_lines.append(
                f"- field_id={fld.field_id} Layer{layer} {key}={fld.value} "
                f"(conf {fld.confidence:.2f}, evidence×{len(fld.evidence_refs)}, "
                f"last_updated {fld.last_updated.isoformat()})",
            )
    if not has_any:
        confirmed_lines.append("- （無）")

    safe_pending = [
        cand for cand in pending if not _is_sensitive_candidate(cand)
    ]
    pending_lines = ["[pending candidates]"]
    if not safe_pending:
        pending_lines.append("- （無）")
    else:
        for cand in safe_pending:
            pending_lines.append(
                f"- candidate_id={cand.candidate_id} Layer{cand.layer} "
                f"{cand.field_key}={cand.proposed_value} "
                f"(raw_conf {cand.raw_extractor_confidence:.2f}, "
                f"explicit={cand.explicit}, "
                f"quote={cand.evidence_ref.quote!r})",
            )

    safe_decay_candidates = [
        fld for fld in decay_candidates if not _is_sensitive_field(fld)
    ]
    decay_lines = ["[超過 decay 期限的 confirmed 欄位]"]
    if not safe_decay_candidates:
        decay_lines.append("- （無）")
    else:
        for fld in safe_decay_candidates:
            decay_lines.append(
                f"- field_id={fld.field_id} Layer{fld.layer} "
                f"{fld.field_key}={fld.value} "
                f"(conf {fld.confidence:.2f}, last_updated "
                f"{fld.last_updated.isoformat()})",
            )

    return get_default_loader().render(
        "persona/consolidator",
        confirmed_block="\n".join(confirmed_lines),
        pending_block="\n".join(pending_lines),
        decay_block="\n".join(decay_lines),
    )


def _is_sensitive_candidate(candidate: CandidateField) -> bool:
    return candidate.content_mode is MessageContentMode.NSFW


def _is_sensitive_field(field: ProfileField) -> bool:
    return field.content_mode is MessageContentMode.NSFW


def _parse_response(
    raw: str,
    *,
    candidate_by_id: dict[str, CandidateField],
    valid_field_ids: set[str],
    confirmed_by_id: dict[str, ProfileField],
) -> ConsolidationResult:
    obj = _extract_object(raw)
    if obj is None:
        return ConsolidationResult()
    actions_raw = obj.get("actions")
    if not isinstance(actions_raw, list):
        return ConsolidationResult()
    result = ConsolidationResult()
    for entry in actions_raw:
        if not isinstance(entry, dict):
            continue
        action_type = str(entry.get("type") or "").strip().lower()
        if action_type == "promote":
            promo = _parse_promote(entry, candidate_by_id)
            if promo is not None:
                result.promotions.append(promo)
        elif action_type == "merge":
            merge = _parse_merge(entry, candidate_by_id)
            if merge is not None:
                result.merges.append(merge)
        elif action_type == "supersede":
            sup = _parse_supersede(
                entry, candidate_by_id, confirmed_by_id,
            )
            if sup is not None:
                result.supersedes.append(sup)
        elif action_type == "reject":
            rej = _parse_reject(entry, set(candidate_by_id))
            if rej is not None:
                result.rejections.append(rej)
        elif action_type == "decay":
            decay = _parse_decay(entry, valid_field_ids)
            if decay is not None:
                result.decays.append(decay)
        elif action_type == "infer":
            infer = _parse_infer(entry, confirmed_by_id)
            if infer is not None:
                result.inferences.append(infer)
    return result


def _parse_promote(
    entry: dict, candidate_by_id: dict[str, CandidateField],
) -> PromoteAction | None:
    cand_id = str(entry.get("candidate_id") or "").strip()
    cand = candidate_by_id.get(cand_id)
    if cand is None:
        return None
    field_key = str(entry.get("field_key") or "").strip().lower()
    if not field_key:
        return None
    try:
        layer = int(entry.get("layer"))
    except (TypeError, ValueError):
        return None
    if layer not in _VALID_LAYERS:
        return None
    if not _valid_layer_key(layer, field_key):
        return None
    if cand.layer != layer or cand.field_key != field_key:
        return None
    value = str(entry.get("value") or "").strip()
    if not value:
        return None
    new_conf = _coerce_confidence(entry.get("new_confidence"), layer)
    if new_conf is None:
        return None
    reason_raw = entry.get("reason")
    reason = (
        str(reason_raw).strip() if isinstance(reason_raw, str) and reason_raw.strip() else None
    )
    return PromoteAction(
        candidate_id=cand_id,
        field_key=field_key,
        layer=layer,
        value=value,
        new_confidence=new_conf,
        reason=reason,
    )


def _parse_merge(
    entry: dict, candidate_by_id: dict[str, CandidateField],
) -> MergeAction | None:
    raw_ids = entry.get("candidate_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return None
    ids: list[str] = []
    for rid in raw_ids:
        sid = str(rid or "").strip()
        if sid and sid in candidate_by_id:
            ids.append(sid)
    if len(ids) < 2:
        return None
    field_key = str(entry.get("field_key") or "").strip().lower()
    if not field_key:
        return None
    try:
        layer = int(entry.get("layer"))
    except (TypeError, ValueError):
        return None
    if layer not in _VALID_LAYERS:
        return None
    if not _valid_layer_key(layer, field_key):
        return None
    for sid in ids:
        cand = candidate_by_id[sid]
        if cand.layer != layer or cand.field_key != field_key:
            return None
    value = str(entry.get("value") or "").strip()
    if not value:
        return None
    new_conf = _coerce_confidence(entry.get("new_confidence"), layer)
    if new_conf is None:
        return None
    reason_raw = entry.get("reason")
    reason = (
        str(reason_raw).strip() if isinstance(reason_raw, str) and reason_raw.strip() else None
    )
    return MergeAction(
        candidate_ids=tuple(ids),
        field_key=field_key,
        layer=layer,
        value=value,
        new_confidence=new_conf,
        reason=reason,
    )


def _parse_supersede(
    entry: dict,
    candidate_by_id: dict[str, CandidateField],
    confirmed_by_id: dict[str, ProfileField],
) -> SupersedeAction | None:
    field_id = str(entry.get("superseded_field_id") or "").strip()
    existing = confirmed_by_id.get(field_id)
    if existing is None:
        return None
    raw_ids = entry.get("candidate_ids") or []
    if not isinstance(raw_ids, list):
        return None
    ids = [
        str(rid).strip()
        for rid in raw_ids
        if isinstance(rid, str) and str(rid).strip() in candidate_by_id
    ]
    if not ids:
        return None
    field_key = str(entry.get("field_key") or "").strip().lower()
    if not field_key:
        return None
    try:
        layer = int(entry.get("layer"))
    except (TypeError, ValueError):
        return None
    if layer not in _VALID_LAYERS:
        return None
    if not _valid_layer_key(layer, field_key):
        return None
    if existing.layer != layer or existing.field_key != field_key:
        return None
    for sid in ids:
        cand = candidate_by_id[sid]
        if cand.layer != layer or cand.field_key != field_key:
            return None
    new_value = str(entry.get("new_value") or "").strip()
    if not new_value:
        return None
    new_conf = _coerce_confidence(entry.get("new_confidence"), layer)
    if new_conf is None:
        return None
    reason = str(entry.get("reason") or "").strip()
    if not reason:
        return None
    # Layer 1 supersede needs ≥ 2 candidates as backing — identity
    # facts (name/age/occupation) shouldn't flip on a single mention.
    if layer == 1 and len(ids) < 2:
        return None
    return SupersedeAction(
        superseded_field_id=field_id,
        candidate_ids=tuple(ids),
        field_key=field_key,
        layer=layer,
        new_value=new_value,
        new_confidence=new_conf,
        reason=reason,
    )


def _parse_reject(
    entry: dict, valid_candidate_ids: set[str],
) -> RejectAction | None:
    cand_id = str(entry.get("candidate_id") or "").strip()
    if cand_id not in valid_candidate_ids:
        return None
    reason = str(entry.get("reason") or "").strip() or "no reason"
    return RejectAction(candidate_id=cand_id, reason=reason)


def _parse_decay(
    entry: dict, valid_field_ids: set[str],
) -> DecayAction | None:
    field_id = str(entry.get("field_id") or "").strip()
    if field_id not in valid_field_ids:
        return None
    new_conf_raw = entry.get("new_confidence")
    try:
        new_conf = float(new_conf_raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= new_conf <= 1.0:
        return None
    reason = str(entry.get("reason") or "").strip() or "decay"
    return DecayAction(field_id=field_id, new_confidence=new_conf, reason=reason)


def _parse_infer(
    entry: dict, confirmed_by_id: dict[str, ProfileField],
) -> InferAction | None:
    field_key = str(entry.get("field_key") or "").strip().lower()
    if not field_key:
        return None
    try:
        layer = int(entry.get("layer"))
    except (TypeError, ValueError):
        return None
    if layer not in _VALID_LAYERS:
        return None
    if not _valid_layer_key(layer, field_key):
        return None
    for fld in confirmed_by_id.values():
        if fld.layer == layer and fld.field_key == field_key:
            return None
    value = str(entry.get("value") or "").strip()
    if not value:
        return None
    new_conf_raw = entry.get("new_confidence")
    try:
        new_conf = float(new_conf_raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= new_conf <= _MAX_INFER_CONFIDENCE:
        return None
    reason = str(entry.get("reason") or "").strip()
    if not reason:
        return None
    supporting_raw = entry.get("supporting_field_ids") or []
    supporting = (
        tuple(
            str(sid).strip()
            for sid in supporting_raw
            if (
                isinstance(sid, (str, int))
                and str(sid).strip() in confirmed_by_id
            )
        )
        if isinstance(supporting_raw, list)
        else ()
    )
    if supporting_raw and not supporting:
        return None
    return InferAction(
        field_key=field_key,
        layer=layer,
        value=value,
        new_confidence=new_conf,
        reason=reason,
        supporting_field_ids=supporting,
    )


def _valid_layer_key(layer: int, field_key: str) -> bool:
    return field_key in _LAYER_FIELD_KEYS.get(layer, frozenset())


def _coerce_confidence(raw: object, layer: int) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 1.0:
        return None
    max_conf = _LAYER_MAX_CONFIDENCE.get(layer, 0.95)
    if value > max_conf:
        return max_conf
    return value


def _extract_object(raw: str) -> dict | None:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i, ch in enumerate(text[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
