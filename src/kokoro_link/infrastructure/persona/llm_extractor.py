"""LLM-backed persona extractor.

After each chat turn the application service hands us the user's
latest message (plus a few prior turns for context) and the current
confirmed persona. We prompt the model to spot any operator-side
facts and return them with verbatim quotes — those quotes are the
anti-hallucination receipt the application service checks before
writing to staging.

Why a dedicated call (instead of folding into post-turn):

- Post-turn already balances memory + state + arc; mixing operator
  facts diluted accuracy in early tests.
- The schema here is open-ended (≈25 field_keys across four layers);
  a focused prompt + small JSON schema produces cleaner output.
- Different LLM routing — operators can pin a cheap observation
  model here while keeping post-turn on a stronger reasoner.

LLM-first rules: NO keyword matching, NO regex special cases inside
this module. Validation is statistical (confidence threshold,
substring guard, layer-eligibility) — never semantic.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from kokoro_link.application.services.feature_keys import FEATURE_PERSONA_EXTRACT
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_extractor import PersonaExtractorPort
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.resolved_address import ResolvedAddress
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader

_LOGGER = logging.getLogger(__name__)

_MAX_HISTORY_TURNS = 6
_MAX_HISTORY_CHARS_PER_TURN = 300
_MAX_CANDIDATES = 8
_MAX_VALUE_CHARS = 200
_MAX_QUOTE_CHARS = 200
_MIN_SELF_CONF = 0.5

# Layer 4 is excluded — it's computed, not extracted. The extractor
# must not produce it (validation drops layer=4 candidates).
_EXTRACTABLE_LAYERS: frozenset[int] = frozenset({1, 2, 3, 5})

# Field-key dictionary per layer. Used for prompt rendering and for
# silently dropping LLM hallucinations of unsupported keys. Adding a
# key is intentional — it must be reflected in the prompt rules too.
_LAYER_FIELD_KEYS: dict[int, tuple[str, ...]] = {
    1: (
        "name", "nickname", "age", "occupation",
        "company_or_school", "residence", "family",
    ),
    2: (
        "interests", "diet", "routine", "consumption_style",
        "income_band", "relationship_status", "life_goals",
    ),
    3: (
        "anxieties", "traumas", "secrets", "vulnerabilities",
        "values", "openness_level",
    ),
    5: (
        "money_borrowed", "help_asked", "vulnerability_shown",
        "family_introduced", "resource_shared", "secret_kept",
    ),
}

_LAYER_FIELD_KEY_SET: frozenset[tuple[int, str]] = frozenset(
    (layer, key)
    for layer, keys in _LAYER_FIELD_KEYS.items()
    for key in keys
)

# Layer-1 fields that NAME the operator — these surface as the player's
# identity in the memoir/projection, so a third party's name landing here
# is the worst contamination. They require an explicit operator_self
# attribution (see ``_parse_candidate``).
_IDENTITY_FIELD_KEYS: frozenset[str] = frozenset({"name", "nickname"})

# Subject labels that are NOT a confident self-attribution. A candidate
# explicitly carrying one of these is never banked as the operator's own
# fact: ``other_person``/``character`` name a third party, and ``unclear``
# is the model's own hedge that it can't pin the quote to the operator —
# a fact we can't attribute to the operator must not become the operator's.
# (A *missing* subject is still tolerated for ordinary descriptive fields
# so coverage isn't lost; only the identity / sensitive layers demand a
# positive ``operator_self``.)
_NON_SELF_SUBJECTS: frozenset[str] = frozenset(
    {"other_person", "character", "unclear"},
)


class LLMPersonaExtractor(PersonaExtractorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model,
            feature_key=FEATURE_PERSONA_EXTRACT,
        )

    async def extract(
        self,
        *,
        character_id: str,
        operator: OperatorProfile,
        current_persona: OperatorPersona,
        conversation_id: str,
        user_message_id: str,
        user_message: str,
        assistant_message: str,
        recent_messages: list[Message] | None = None,
        resolved_player_address: ResolvedAddress | None = None,
    ) -> list[CandidateField]:
        if await self._resolver.is_fake():
            # Fake LLM emits junk that won't parse — short-circuit so
            # we don't pollute staging with garbage candidates.
            return []
        prompt = _build_prompt(
            operator=operator,
            current_persona=current_persona,
            user_message=user_message,
            assistant_message=assistant_message,
            recent_messages=recent_messages or [],
            resolved_player_address=resolved_player_address,
        )
        try:
            # Cloud identity is supplied by the ambient cloud actor bound at
            # the request / dream-pass boundary (see cloud_identity_context),
            # so this operator-scoped extractor need not thread an
            # operator_id of its own.
            raw = await self._resolver.generate(prompt)
        except Exception:
            _LOGGER.exception("Persona extraction LLM call failed")
            return []
        return _parse_response(
            raw,
            character_id=character_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            user_message=user_message,
            recent_user_messages=tuple(
                msg.content for msg in (recent_messages or [])
                if msg.role == MessageRole.USER
            ),
        )


def _build_prompt(
    *,
    operator: OperatorProfile,
    current_persona: OperatorPersona,
    user_message: str,
    assistant_message: str,
    recent_messages: list[Message],
    resolved_player_address: ResolvedAddress | None = None,
) -> str:
    # Resolved player address (seed > learned persona > display name) names
    # the operator throughout the extraction prompt — so a per-character
    # seed name outranks a raw platform/OAuth label. Falls back to the raw
    # display name (then 對方) when the caller passes no resolution.
    if resolved_player_address is not None and not resolved_player_address.is_fallback:
        operator_label = resolved_player_address.primary
    else:
        operator_label = (
            operator.display_name if operator.has_real_name() else "對方"
        )
    layer_dict_lines = ["可使用的欄位字典（layer:key 形式，禁止使用其他 key）："]
    for layer in sorted(_LAYER_FIELD_KEYS.keys()):
        keys_csv = ", ".join(_LAYER_FIELD_KEYS[layer])
        layer_dict_lines.append(f"  - Layer {layer}: {keys_csv}")
    return get_default_loader().render(
        "persona/extractor",
        # Extracted fact ``value`` surfaces in the player-visible persona
        # projection (PersonaProjectionPanel) and the admin mirror, so it
        # must follow the operator's content language rather than default
        # to Chinese (bug B2 class).
        language_hint=render_operator_language_hint(
            getattr(operator, "primary_language", None),
        ),
        operator_label=operator_label,
        layer_dict_block="\n".join(layer_dict_lines),
        known_block="\n".join(_render_known_persona(current_persona)),
        history_block="\n".join(_render_history(recent_messages, operator_label)),
        user_message=user_message,
        assistant_message=assistant_message,
        max_candidates=_MAX_CANDIDATES,
    )


def _render_known_persona(persona: OperatorPersona) -> list[str]:
    if persona.is_empty():
        return ["[已知的畫像] （目前還是空的，這是第一次抽取）"]
    out = ["[已知的畫像]"]
    for layer in (1, 2, 3, 5):
        fields = persona.fields_by_layer(layer)
        if not fields:
            continue
        for key, fld in fields.items():
            out.append(
                f"  - Layer {layer} {key}: {fld.value} "
                f"(信心 {fld.confidence:.2f})",
            )
    return out


def _render_history(messages: list[Message], operator_label: str) -> list[str]:
    if not messages:
        return ["[近期對話脈絡] （無）"]
    out = ["[近期對話脈絡]（最近輪在最下）"]
    tail = messages[-_MAX_HISTORY_TURNS:]
    for msg in tail:
        content = (msg.content or "").strip()
        if not content:
            continue
        if len(content) > _MAX_HISTORY_CHARS_PER_TURN:
            content = content[:_MAX_HISTORY_CHARS_PER_TURN] + "…"
        label = operator_label if msg.role == MessageRole.USER else "角色"
        out.append(f"- {label}：{content}")
    if len(out) == 1:
        out.append("- （無有效內容）")
    return out


def _parse_response(
    raw: str,
    *,
    character_id: str,
    conversation_id: str,
    user_message_id: str,
    user_message: str,
    recent_user_messages: tuple[str, ...],
) -> list[CandidateField]:
    obj = _extract_object(raw)
    if obj is None:
        return []
    raw_candidates = obj.get("candidates")
    if not isinstance(raw_candidates, list):
        return []
    now = datetime.now(timezone.utc)
    # Recent user messages are context only. Evidence must come from
    # the current turn so the stored turn_id cannot point at the wrong
    # message.
    haystacks = (user_message,)
    out: list[CandidateField] = []
    for entry in raw_candidates[: _MAX_CANDIDATES]:
        cand = _parse_candidate(
            entry,
            haystacks=haystacks,
            character_id=character_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            now=now,
        )
        if cand is not None:
            out.append(cand)
    return out


def _parse_candidate(
    entry: object,
    *,
    haystacks: tuple[str, ...],
    character_id: str,
    conversation_id: str,
    user_message_id: str,
    now: datetime,
) -> CandidateField | None:
    if not isinstance(entry, dict):
        return None
    layer_raw = entry.get("layer")
    try:
        layer = int(layer_raw)
    except (TypeError, ValueError):
        return None
    if layer not in _EXTRACTABLE_LAYERS:
        return None
    field_key = str(entry.get("field_key") or "").strip().lower()
    if (layer, field_key) not in _LAYER_FIELD_KEY_SET:
        return None
    value = str(entry.get("value") or "").strip()
    if not value:
        return None
    if len(value) > _MAX_VALUE_CHARS:
        value = value[: _MAX_VALUE_CHARS]
    quote = str(entry.get("quote") or "").strip()
    if not quote:
        return None
    if len(quote) > _MAX_QUOTE_CHARS:
        quote = quote[: _MAX_QUOTE_CHARS]
    # Substring guard: the quote MUST appear in the operator's actual
    # messages (current turn or recent context). This is the single
    # strongest defence against hallucinated facts.
    if not any(quote in hay for hay in haystacks if hay):
        return None
    self_conf_raw = entry.get("self_confidence")
    try:
        self_conf = float(self_conf_raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= self_conf <= 1.0:
        return None
    if self_conf < _MIN_SELF_CONF:
        return None
    explicit_raw = entry.get("explicit")
    explicit = bool(explicit_raw) if explicit_raw is not None else False
    subject = str(entry.get("subject") or "").strip().lower()
    # Subject discipline — a persona fact must be about the operator, not
    # a third party the operator merely mentioned. The model already
    # classifies ``subject`` for every candidate; we enforce it here.
    #   - Identity-naming fields (name / nickname) and the sensitive
    #     layers (3 / 5) demand an EXPLICIT ``operator_self`` attribution:
    #     a peer character's name must never become the operator's own
    #     name (it would surface as the player's identity in the memoir).
    #   - Other descriptive fields only reject an explicit third-party
    #     attribution, so a missing ``subject`` doesn't silently stop the
    #     extractor from learning ordinary facts (occupation, interests…).
    if layer in {3, 5} or (layer == 1 and field_key in _IDENTITY_FIELD_KEYS):
        if subject != "operator_self":
            return None
    elif subject in _NON_SELF_SUBJECTS:
        return None
    # Layer 5 requires explicit=true. The dream job double-checks but
    # filtering here saves an LLM round trip on obviously bad rows.
    if layer == 5 and not explicit:
        return None
    try:
        evidence = EvidenceRef(
            turn_id=user_message_id,
            conversation_id=conversation_id,
            quote=quote,
            extracted_at=now,
        )
        source = "user_explicit" if (layer == 5 and explicit) else "extraction"
        return CandidateField(
            field_key=field_key,
            layer=layer,
            proposed_value=value,
            evidence_ref=evidence,
            raw_extractor_confidence=self_conf,
            state="pending",
            source=source,
            extracted_at=now,
            explicit=explicit,
            character_id=character_id,
        )
    except ValueError:
        return None


def _extract_object(raw: str) -> dict | None:
    """Pull the first valid JSON object out of an LLM response. The
    model occasionally wraps output in ```json fences or adds a stray
    preamble; this scan tolerates both."""
    if not raw:
        return None
    text = raw.strip()
    # Strip code fences.
    if text.startswith("```"):
        # Drop opening fence line.
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    # Find the first '{' and the matching '}'.
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
    if isinstance(obj, dict):
        return obj
    return None
