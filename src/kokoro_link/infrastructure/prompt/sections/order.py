"""The one table that fixes the chat prompt's section order.

Ordering used to live implicitly in the shape of a 250-line literal list
inside ``build()``: moving a block meant editing the join, and nothing
could state the order without re-reading it. Here it is data.

Two invariants, both pinned by ``tests/unit/prompt_sections``:

* every registered section names an entry in :data:`SECTION_ORDER`, and
  every entry is claimed by exactly one section;
* :data:`SECTION_ORDERS` is strictly increasing in table order, so the
  registry's sort is total and stable.

Order values are spaced by 10 so an out-of-tree registration can slot a
section between two neighbours without renumbering the table.

**Why the table reads stable-first (DH5).** Every upstream we speak to
(OpenRouter, Anthropic, DeepSeek-compatible endpoints) caches on a
*prefix*: the cache survives only up to the first byte that differs from
the previous request. So the table is sorted by how often a block's text
changes, coarsest first — per-operator, then per-character, then the tool
rail, and only then everything a turn re-derives. One volatile block
parked early costs the cache every stable block behind it, which is
exactly what ``turn_register`` (a per-turn ``RegisterProfile``, numbers
and all) was doing from the middle of the character sheet.

Ordering is *content-neutral*: nothing about a block's text depends on
this table except where a block says 上方/下方 out loud. Those claims are
load-bearing and are pinned in the zone comments below — read them before
moving anything.
"""

from types import MappingProxyType
from typing import Final, Mapping

SECTION_ORDER: Final[tuple[str, ...]] = (
    # == the cacheable prefix ========================================
    # -- who is talking to whom (per-operator; rewritten by a rename
    #    or a persona refresh, never by a turn) ---------------------
    "operator_language",
    "operator_identity",
    "address_change",
    "player_persona_note",
    "operator_persona",
    "peer_roster",
    # -- the character sheet (per-character; the longest stable run
    #    in the prompt, so nothing turn-derived may sit inside it) --
    #    ``character_profile`` must precede ``state_behavior``: that
    #    block's 互動界線 line says 觸碰上方「禁忌」, and 禁忌 is a line of
    #    the profile.
    "character_profile",
    "disposition",
    "personality_type",
    "body_state",
    "register",
    "phrase_habit",
    "birthday",
    #    ``knowledge_boundary`` is 「right after the birthday block」 by
    #    its own docstring — persona + age + scope read as one unit.
    "knowledge_boundary",
    #    ``residue`` keeps its prime position (its 24h window exists so
    #    a stale aftermath *stops* crowding the start of the prompt);
    #    it changes only when an aftermath lands or ages out.
    "residue",
    # -- the tool rail (per-capability-set) --------------------------
    #    Hoisted out of the material zone: it is the single largest
    #    block that is *usually* turn-invariant, so leaving it
    #    downstream of the logistics zone meant re-sending it every
    #    turn on the common path. Not an absolute invariant, though: a
    #    forced-tool turn threads ``ctx.tools.forced_tool_name`` into
    #    ``render_tools_block`` (see ``sections/tools.py``), which
    #    appends a per-turn 「這回合必須呼叫」 directive here — that
    #    turn's tools block differs from the previous one, so its
    #    upstream cache prefix breaks at this block same as any other
    #    turn-derived section. Known and accepted: forced-tool turns are
    #    infrequent, and paying for a cache miss there is cheaper than
    #    moving the whole tool rail downstream for the common case it
    #    exists to serve.
    #    ``tool_outcomes`` stays behind in the material zone and says
    #    「上面的工具」 — still true, just further up.
    "tools",
    #    ``honesty_discipline`` (HV2) is the rule the tool rail used to
    #    carry alone — hoisted out because ``render_tools_block`` returns
    #    nothing when no tool is offered, which is precisely the final hop
    #    of a tool turn and every tool-less character. Its baseline text is
    #    constant and its only variable (whether a browsing tool exists for
    #    this character) is per-capability-set, so it belongs here at the
    #    tail of the cacheable prefix rather than below the fold. It says
    #    「這一輪有沒有工具可用」 without pointing at a neighbour, so it has
    #    no positional claim to keep.
    "honesty_discipline",
    #    ``health_care`` (TR3) is constant for the same reason
    #    ``honesty_discipline`` is: nothing about it varies per turn or
    #    per character, so it belongs at the tail of the cacheable
    #    prefix rather than below the fold. It names no neighbour, so it
    #    has no positional claim to keep either.
    "health_care",
    # == everything below is re-derived on most turns ================
    # -- this turn's frame and register ------------------------------
    #    ``presence_frame`` flips with the surface and with whether the
    #    turn carries attachments; ``turn_register`` is a fresh
    #    RegisterProfile (numeric axes + note) on every single turn.
    "presence_frame",
    "turn_register",
    # -- current inner state ----------------------------------------
    "character_state",
    "state_behavior",
    "emotional_overload",
    "direction",
    # -- the scripted scene, above logistics on purpose -------------
    "story_scene",
    "today_scene",
    # -- time, place, logistics -------------------------------------
    "timing",
    "calendar",
    "weather",
    "world_event_context",
    "world_event_recall",
    #    ``schedule`` must precede ``story_events`` and
    #    ``material_digest``: both defer to 上方「行程」段 when their
    #    material contradicts where the character actually is.
    "schedule",
    "completed_today",
    "pending_invites",
    "upcoming_days",
    # -- material and narrative -------------------------------------
    #    ``material_digest`` is the stand-in for the five blocks it
    #    suppresses (registry.DIGEST_SUPPRESSED_SECTIONS) and leads
    #    them; ``recent_feed``, the fifth, sits in the conversation
    #    zone where the rest of the recall rails live.
    "material_digest",
    "emotion_events",
    "self_reflection",
    "tool_outcomes",
    "story_events",
    "story_arc",
    "arc_history",
    # -- the conversation itself ------------------------------------
    #    ``vision_legend`` numbers 下方對話 and ``image_recognition``
    #    describes 上述圖片 in that numbering — the pair stays adjacent
    #    and above the transcript, never after the footer.
    "conversation_id",
    "vision_legend",
    "image_recognition",
    "older_dialogue",
    "recent_proactive",
    "recent_feed",
    "recent_self_lines",
    "self_repetition",
    "diversity_evidence",
    "persona_self_check",
    "persona_curiosity",
    "recent_dialogue",
    "relationship_milestones",
    "memory",
    "initial_relationship",
    "relationship_anchor",
    # -- this turn's tail -------------------------------------------
    #    Recency-load-bearing from ``recent_dialogue`` down; the footer
    #    quotes half a dozen 上方 sections by name. Nothing here moves
    #    for cache reasons.
    "stage_nudge",
    "latest_user_message",
    "retry_directive",
    "instructions_footer",
)

SECTION_ORDER_STEP: Final[int] = 10

SECTION_ORDERS: Final[Mapping[str, int]] = MappingProxyType(
    {
        name: (index + 1) * SECTION_ORDER_STEP
        for index, name in enumerate(SECTION_ORDER)
    }
)
