"""Canonical feature keys and groups for LLM routing.

Every auxiliary LLM service is tagged with one of these when it's
wired in the container. The ``ActiveLLMProviderPort`` uses the tag to
look up a feature-specific override in the ``feature_models``
preference; if nothing is set for that key, it falls back to the
global ``active_model`` preference, then to the container's default
provider id.

Strings rather than an enum so the JSON preference payload stays
portable — the frontend can write ``{"post_turn": {"provider_id":
"anthropic", "model_id": "claude-opus-4-7"}}`` without importing a
Python enum. Unknown keys are treated as "no override" by the port,
so typos in the pref degrade gracefully.
"""

from __future__ import annotations


FEATURE_CHAT = "chat"
"""Main user-facing reply generation.

Meaningful at both the global and **per-character** levels. A global
``feature_models[chat]`` pins the main chat route explicitly, while
``active_model`` remains the fallback for unpinned features. Per
character, ``feature_models[chat]`` overrides the global chat pick so
operators can wire e.g. character A to Anthropic Sonnet and character B
to LM Studio without flipping the global dropdown every time."""


FEATURE_IMAGE_RECOGNITION = "image_recognition"
"""User-uploaded image recognition for text-only downstream models.

When the player sends chat images or uploads an image for character
drafting but the resolved final model does not support vision, the
caller can route those images here first. The multimodal model turns
them into detailed textual context; the original chat or draft model
still writes the final user-facing output."""


FEATURE_POST_TURN = "post_turn"
"""Memory extraction + state suggestion + schedule/arc adjustments."""

FEATURE_GOAL_REVIEW = "goal_review"
"""Periodic character-goal reviewer (every N turns)."""

FEATURE_SCHEDULE_PLAN = "schedule_plan"
"""Daily schedule planner (first chat of the civil day)."""

FEATURE_ARC_PLAN = "arc_plan"
"""Multi-week story arc planner (first arc + stale-arc regeneration)."""

FEATURE_ARC_SEASON_DECIDE = "arc_season_decide"
"""Dormant story-arc season opener decider.

Judges whether a character who just completed an arc should stay in
daily-life whitespace or begin the next LLM-planned season now.
"""

FEATURE_ARC_BEAT_RECHECK = "arc_beat_recheck"
"""Semantic recheck for due beats repeatedly surfaced without landing."""

FEATURE_ARC_SCENE_WRITE = "arc_scene_write"
"""Autonomous short-scene writer for due story-arc beats.

Direction C uses this to turn a pending beat into a performed scene
that can land as a StoryEvent even when the user is not available.
"""

FEATURE_ARC_COMPLETION_MEMORY = "arc_completion_memory"
"""Relationship milestone memory writer when an arc completes."""

FEATURE_STORY_EXPAND = "story_expand"
"""Daily gacha → first-person narrative expansion."""

FEATURE_MEMORY_CONSOLIDATE = "memory_consolidate"
"""LLM-backed memory cluster merger."""

FEATURE_DIALOGUE_SUMMARY = "dialogue_summary"
"""Recent-dialogue condensation feeding schedule / arc / proactive
prompts with "what they've been talking about"."""

FEATURE_NSFW_SAFE_SUMMARY = "nsfw_safe_summary"
"""Restricted-message safety rewrite used before frontier-model prompts.

This is reported in the usage ledger but is not independently routed in
``GLOBAL_FEATURE_KEYS`` yet because it intentionally reuses the active
chat model for the current turn.
"""

FEATURE_PROMPT_REWRITE = "prompt_rewrite"
"""Chinese → danbooru tag translator used by the image tool."""


FEATURE_PROMPT_MATERIAL_DIGEST = "prompt_material_digest"
"""Prompt-material digester for chat-context poetic material.

Runs before main chat generation when enabled. It turns emotion,
self-reflection, story, and feed material into fact bullets so the main
chat model sees continuity facts without treating poetic source text as
a style sample.
"""


FEATURE_NOVELTY_GATE = "novelty_gate"
"""Post-generation reply quality gate.

Runs after a candidate player-visible reply when enabled. It judges
novelty, imagery relapse, register mismatch, over-warmth, and formulaic
tone before the caller optionally retries once.
"""


FEATURE_REGISTER_PROFILE = "register_profile"
"""Per-turn register profiler for player-visible reply surfaces.

Runs before generation to produce soft semantic axes for the current
conversation register. The profile conditions generation and gives the
reply quality gate context without using hard scene labels.
"""


FEATURE_IMAGE_CHAT_TOOL = "image_chat_tool"
"""Image profile routing for the in-chat ``generate_image`` tool.

Distinct from :data:`FEATURE_PROMPT_REWRITE` — that one picks the LLM
that translates Chinese scenes into danbooru tags; this one picks
*which image backend* runs the actual diffusion / API call. Operators
typically pin this per-character so an anime persona renders against
an Illustrious profile while a realistic persona renders against a
Pony / SDXL-realistic profile."""


FEATURE_IMAGE_PORTRAIT = "image_portrait"
"""Image profile routing for the character-settings 'generate
portrait' button + gacha candidate flow."""


FEATURE_IMAGE_FEED = "image_feed"
"""Image profile routing for LumeGram feed post images."""


FEATURE_VIDEO_FEED = "video_feed"
"""Video profile routing for LumeGram feed post short clips.

Scoped to feed-only for now — chat-tool clips and branching-drama
scene videos are future surfaces that'll add their own keys when /
if we decide to ship them. Wan2.2 generation takes minutes and costs
real GPU time, so each new surface needs an explicit budget decision."""

FEATURE_ARC_TEMPLATE_INTAKE = "arc_template_intake"
"""Wizard-driven arc template authoring (Phase 2.7). Per-step LLM
suggestions: title/theme/tone, premise condensation, beat options,
summary writing, full-draft generation."""

FEATURE_CHARACTER_DRAFT = "character_draft"
"""AI-assisted character draft generation.

Low-frequency but player-visible: names, summaries, voice profile, and
initial character facts should use a model with good creative taste.
"""

FEATURE_CHARACTER_PERSONALITY_TYPE = "character_personality_type"
"""16 型性格 analyzer for character creation/edit consistency checks."""

FEATURE_CHARACTER_CREATION_INTAKE = "character_creation_intake"
"""Relationship/profile gap analyzer used by the character creation wizard."""

FEATURE_ARC_ADAPT = "arc_adapt"
"""Fusion-story to arc-template semantic adapter.

Reads a completed multi-character short story and produces an unsaved
``TemplateDraft`` for wizard review. Separate from both
``fusion_story`` and ``arc_template_intake`` because this is a heavy
cross-format reasoning call, not story generation or stepwise drafting.
"""

FEATURE_ARC_CONTINUATION_DRAFT = "arc_continuation_draft"
"""Concluded ArcSeries to next-season authoring draft.

Reads runtime context after a fixed series is concluded and returns an
unsaved ``TemplateDraft`` for wizard review. Kept separate from runtime
season opening so the model cannot silently mutate progress.
"""

FEATURE_FEED_COMPOSE = "feed_compose"
"""Character feed-wall post authoring (LumeGram). Turns a curated
candidate signal — schedule activity, story beat, memory, world
event, or derived state — into a short first-person post and (when
applicable) a positive image prompt for ComfyUI."""

FEATURE_FEED_COMMENT_REPLY = "feed_comment_reply"
"""Character → user reply on a LumeGram post comment thread.
Phase B: scheduler-tick driven, fires only when the character is not
high-busy and within the per-day cap. Independent budget from
``feed_compose`` so a heavy reply day can't block fresh posts."""

FEATURE_ACTIVITY_AFTERMATH = "activity_aftermath"
"""Per-schedule-activity emotional residue judge. Runs at memorialisation
time on each completed activity — reads persona + activity + companions
+ busy_score and outputs a short Chinese residue line ("被一直追問感情很
煩躁") plus an optional one-word mood tag. Folded into the episodic
memory's content so the next chat naturally surfaces the feeling, and
tagged ``aftermath`` so the prompt builder can promote fresh ones to a
dedicated 情緒尾韻 block. Cheap, short context — fits a small/fast model."""

FEATURE_IDLE_DRIFT = "idle_drift"
"""Long-absence mood drift judge. Runs at chat pre-turn time when the
user has been idle past a threshold (default 2h) — reads persona axes
+ idle duration and outputs a small emotion override / affection delta
in a personality-appropriate direction (tsundere sulks, clingy gets
sad, indifferent barely notices). Fed into ``pending_state`` before
prompt build so the next reply naturally reflects the drift. Cheap,
short context — fits a small/fast model."""

FEATURE_BUSY_REPLY_DECIDE = "busy_reply_decide"
"""Busy-defer decider — judges whether a user message arriving during a
high-busy_score activity should get a normal reply, or a brief in-
character "I'll get back to you" with the actual reply deferred. LLM
reads persona + activity + message and writes its own call; no busy-
score thresholds or category lists are enumerated. Cheap, short
context — fits a small/fast model."""

FEATURE_SCENE_ACCESS = "scene_access"
"""Scene Access judge. Decides whether same-space stage interaction is
plausible in the current semantic context, or whether the UI should
prefer phone, asking to meet, or waiting for a more open scene."""

FEATURE_BUSY_FOLLOW_UP = "busy_follow_up"
"""Deferred-reply composer — runs at proactive-tick time once the
character's ``busy_score`` has dropped, takes the queued user messages
+ the brief ack the user already saw and writes the actual full reply.
Uses the same model class as chat reply by default (the output is the
real reply the user has been waiting for), but kept as its own key so
operators can pin a heavier model here if they like."""

FEATURE_SCHEDULED_PROMISE = "scheduled_promise"
"""Scheduled-promise composer — runs at proactive-tick time when the
user explicitly asked the character to message them at a specific
future time ("明天 10 點叫我起床"). The composer interprets the
``promise_intent`` through the character's persona and writes the
actual outbound. Separate from busy-follow-up because the prompt is
shorter and tighter (no queued backlog to wrap up); operators may
prefer a cheap fast model here."""

FEATURE_PROACTIVE_INTENTION = "proactive_intention"
"""Pre-send proactive intention judge. Runs after cheap proactive gates
but before message composition, asking whether the character has a real
inner motive and conversational purpose worth spending today's proactive
slot now. This is a reasoning-heavy call; operators often pin it to a
stronger model than ordinary short-form composers."""

FEATURE_TTS_TRANSLATE = "tts_translate"
"""Pre-TTS dubbing translator. When the character voice is native to
language A but the chat reply is in language B, the translator
rewrites the reply into A so GPT-SoVITS runs same-language synthesis
(no accent / prompt-echo from cross-language inference). Cheap, short
context — operators usually pin this to a small/fast model."""


FEATURE_CARD_TRANSLATE = "card_translate"
"""Character-card import translator.

When a player previews/imports a shared ``.lumecard``, this optional
pre-step translates only the portable A-layer profile prose into the
player's primary language. It is kept separate from chat and TTS so
owners can route this short JSON transformation to a small/fast model."""


FEATURE_ARC_TEMPLATE_TRANSLATE = "arc_template_translate"
"""Arc-template prose translator.

When an operator previews or binds a shipped / community arc template
whose authored language differs from their primary language, this
optional pre-step translates only the player-visible prose fields
(title / premise / each beat's title, summary, location,
scene_characters, dramatic_question) into their language. Structural
fields (theme / tone / tension / scene_type / day_offset / required /
world_frames) never enter the payload. Kept separate from chat / TTS /
card translation so owners can route this short JSON transformation to
a small/fast model — same rationale as ``card_translate``."""


FEATURE_STORY_SEED_TRANSLATE = "story_seed_translate"
"""Story-seed one-liner translator.

A one-shot CLI-time step: when ``cli.import_story_seeds --translate`` is
run, this optionally translates each bundled seed's one-line prompt into
the operator's primary language so the seed management UI reads natively.
Batch, fail-soft, short JSON — routed to a small/fast model like the
other language transforms. The runtime expander already localizes
generated output, so this only touches the management-side readability."""


FEATURE_SILLYTAVERN_NORMALIZE = "sillytavern_normalize"
"""SillyTavern card import free-text normalizer.

When a player imports a SillyTavern character card, this optional step
turns the card's free-text ``description`` / ``personality`` / ``scenario``
/ ``mes_example`` into Core's structured ``personality[]`` / ``interests[]``
/ ``boundaries[]`` / ``appearance`` / ``speaking_style`` fields. Kept
separate from chat and the card translator so owners can route this short
JSON transformation to a small/fast model."""


FEATURE_MEMOIR_LOCALIZE = "memoir_localize"
"""Player-visible memoir localizer.

Runs only on the read side of the Memoir page to render existing memoir
chapters and timeline summaries in the operator's primary language. It
does not create new memories or reflections; it preserves the existing
view structure and only localizes visible prose."""


FEATURE_FUSION_STORY = "fusion_story"
"""Multi-character fusion short-story author.

Drives a 4-stage LLM pipeline (character brief → outline → per-beat
expansion → polish) that produces a 2~3k 字 short story across selected
characters. Heavy generation tier — operators usually pin this to their
strongest available model. Independent from ``story_expand`` (single-
character daily narrative gacha) so the two can run on different models
without one starving the other."""


FEATURE_FUSION_STORY_CRITIC = "fusion_story_critic"
"""Fusion-story polish-loop critic.

A lighter "reviewer" LLM that reads the polished draft and decides
whether another polish round is justified — and if so, points at the
specific paragraphs / phrases to fix. Kept separate from
``fusion_story`` so operators can pin a cheap, fast model to the critic
while the heavy generation model handles the writer/polisher. The polish
loop terminates when the critic returns ``severity == 0`` or the
orchestrator's round cap kicks in."""


FEATURE_BRANCHING_DRAMA = "branching_drama"
"""Branching drama (分歧劇場) — pre-generated branching tree.

Drives a multi-layer LLM pipeline that generates a full branching story
tree (3 tonal variants per segment: dark / sunny / neutral). At runtime,
a lighter LLM call classifies player input and narrates scenes. Separate
from ``fusion_story`` — drama trees are interactive, fusion stories are
read-only prose."""


FEATURE_CHAT_REPETITION_CHECK = "chat_repetition_check"
"""Periodic chat self-repetition extractor.

Background task that runs every N chat turns: reads the character's
most recent assistant replies, asks the model to name any phrasing /
topic / opening patterns the character is starting to over-use, and
caches the resulting hint for injection into the next turn's prompt as
an anti-repetition rail. Independent feature key so operators can pin
a cheap/fast model here while the main chat path stays on the heavy
model — the extractor's job is pattern recognition over short prose,
not creative writing."""


FEATURE_CHAT_ASSIST = "chat_assist"
"""Player-side chat starter assistance.

Generates a few candidate user utterances from recent dialogue,
schedule, character state, story arc, and world-event context. Kept
separate from the main chat key because the output is a short UI aid
for the player, not the character's canonical reply."""


FEATURE_PERSONA_EXTRACT = "persona_extract"
"""Operator-persona extractor — runs after each chat turn in a separate
LLM call to harvest layer 1-5 facts about the operator from the user's
latest message. Independent from :data:`FEATURE_POST_TURN` so operators
can pin a cheaper/observation-tuned model here without weakening the
main memory+state pass."""


FEATURE_PERSONA_DREAM = "persona_dream"
"""Operator-persona dream consolidator — runs periodically (quiet hours)
to promote / merge / supersede / decay accumulated persona candidates.
Usually wants a stronger reasoning model than the extractor since it
weighs evidence across the whole staging buffer; deployments can route
it to a different provider via the feature picker."""


FEATURE_PERSONA_PROJECTION = "persona_projection"
"""Player-facing operator-persona projection.

Reads the per-character persona aggregate and asks an LLM to turn only
the safe Layer 1/2 subset into the "how she sees you" memoir surface.
Kept separate from extraction/dream so deployments can pin a small,
warm prose model without changing the background consolidation path."""


FEATURE_PERSONA_CURIOSITY = "persona_curiosity"
"""Conversational persona discovery planner.

Reads safe persona context and recent curiosity attempts, then decides
whether this turn should naturally carry one low-pressure intent to
learn about the operator. It does not write persona facts; answers
still flow through the existing persona extraction/dream pipeline."""


FEATURE_ADDRESS_PREFERENCE_OBSERVER = "address_preference_observer"
"""Operator address/register observer (§4.2).

Runs from the dream-pass tail stage and asks the model to infer how the
operator tends to address this character (salutation, formality, reply
length preference). Kept separate from persona extraction because it is
short, observation-heavy, and may be pinned to a cheaper model."""


FEATURE_RELATIONSHIP_COHERENCE = "relationship_coherence"
"""Dream-time relationship-coherence self-heal detector.

Runs from the dream-pass tail. Given authoritative address/identity facts
(seed, rename-log, character name, operator profile) + a windowed raw
transcript, and the suspect derived stores (persona name/nickname,
observed salutation, recent memory participants), it judges which derived
values were contaminated by a direction inversion and returns a
structured repair plan. High-reasoning judgement task with a strict
"cite the contradicted authority + only repair on high confidence"
rubric, so it belongs with the high-reasoning gates and can be pinned to
a stronger model independently of the dream consolidator."""


FEATURE_EXPERIMENT_ANALYSIS = "experiment_analysis"
"""Manual A/B experiment analysis (§4.6).

Operator-triggered, high-tier narrative analysis over sticky-bucket
metadata. The service explicitly forbids winner declarations; this key
exists so owners can route the rare batch analysis to a stronger model
without changing chat / dream routing."""


FEATURE_CHARACTER_ENCOUNTER_PLAN = "character_encounter_plan"
"""Role-to-role encounter planner. Judges whether an enabled pair should
naturally meet inside their rolling schedules and proposes a time/place."""


FEATURE_CHARACTER_ENCOUNTER_DIALOGUE = "character_encounter_dialogue"
"""Dedicated short dialogue runner for real character encounters."""


FEATURE_CHARACTER_ENCOUNTER_BEATS = "character_encounter_beats"
"""Encounter topic-beat planner.

Runs once before the encounter dialogue: reads both speakers' contexts
(own recent life, peer knowledge, "already discussed" history) and picks
1-3 concrete topic beats for this meetup, steering away from recently
repeated topics. Distinct from ``character_encounter_plan`` — that key
decides whether/when/where a pair meets; this one decides what the
meeting is actually about so the per-line dialogue model has a fresh
direction instead of orbiting the trigger reason."""


FEATURE_CHARACTER_ENCOUNTER_REFLECT = "character_encounter_reflect"
"""Encounter reflection pass. Converts transcript into per-character
memory summaries, relationship deltas, and hearsay entries."""


FEATURE_PEER_KNOWLEDGE_CONSOLIDATE = "peer_knowledge_consolidate"
"""Character social knowledge consolidation.

Reads relationship/encounter memories for one observer -> peer pair and
updates the stable CharacterPeerProfile used in chat rosters."""


FEATURE_BRANCHING_DRAMA_CRITIC = "branching_drama_critic"
"""Branching-drama narration critic + polisher.

Single-round critic→polish pass applied after each ``narrate()`` to
catch repetition with prior turns, repeated phrasing, abstract drift,
and tone mismatches. Kept on its own feature key so operators can pin a
cheap/fast model to the review pass while the heavier
``branching_drama`` model handles initial narration. Skipped at runtime
when the critic returns ``severity == 0``."""


GLOBAL_FEATURE_KEYS: tuple[str, ...] = (
    FEATURE_CHAT,
    FEATURE_IMAGE_RECOGNITION,
    FEATURE_POST_TURN,
    FEATURE_GOAL_REVIEW,
    FEATURE_SCHEDULE_PLAN,
    FEATURE_ARC_PLAN,
    FEATURE_ARC_SEASON_DECIDE,
    FEATURE_ARC_BEAT_RECHECK,
    FEATURE_ARC_SCENE_WRITE,
    FEATURE_ARC_COMPLETION_MEMORY,
    FEATURE_STORY_EXPAND,
    FEATURE_MEMORY_CONSOLIDATE,
    FEATURE_DIALOGUE_SUMMARY,
    FEATURE_PROMPT_REWRITE,
    FEATURE_PROMPT_MATERIAL_DIGEST,
    FEATURE_NOVELTY_GATE,
    FEATURE_REGISTER_PROFILE,
    FEATURE_CHARACTER_DRAFT,
    FEATURE_CHARACTER_PERSONALITY_TYPE,
    FEATURE_CHARACTER_CREATION_INTAKE,
    FEATURE_ARC_TEMPLATE_INTAKE,
    FEATURE_ARC_ADAPT,
    FEATURE_ARC_CONTINUATION_DRAFT,
    FEATURE_FEED_COMPOSE,
    FEATURE_FEED_COMMENT_REPLY,
    FEATURE_ACTIVITY_AFTERMATH,
    FEATURE_IDLE_DRIFT,
    FEATURE_BUSY_REPLY_DECIDE,
    FEATURE_SCENE_ACCESS,
    FEATURE_BUSY_FOLLOW_UP,
    FEATURE_SCHEDULED_PROMISE,
    FEATURE_PROACTIVE_INTENTION,
    FEATURE_TTS_TRANSLATE,
    FEATURE_CARD_TRANSLATE,
    FEATURE_ARC_TEMPLATE_TRANSLATE,
    FEATURE_STORY_SEED_TRANSLATE,
    FEATURE_SILLYTAVERN_NORMALIZE,
    FEATURE_MEMOIR_LOCALIZE,
    FEATURE_FUSION_STORY,
    FEATURE_FUSION_STORY_CRITIC,
    FEATURE_BRANCHING_DRAMA,
    FEATURE_BRANCHING_DRAMA_CRITIC,
    FEATURE_CHAT_REPETITION_CHECK,
    FEATURE_CHAT_ASSIST,
    FEATURE_PERSONA_EXTRACT,
    FEATURE_PERSONA_DREAM,
    FEATURE_PERSONA_PROJECTION,
    FEATURE_PERSONA_CURIOSITY,
    FEATURE_ADDRESS_PREFERENCE_OBSERVER,
    FEATURE_RELATIONSHIP_COHERENCE,
    FEATURE_EXPERIMENT_ANALYSIS,
    FEATURE_CHARACTER_ENCOUNTER_PLAN,
    FEATURE_CHARACTER_ENCOUNTER_BEATS,
    FEATURE_CHARACTER_ENCOUNTER_DIALOGUE,
    FEATURE_CHARACTER_ENCOUNTER_REFLECT,
    FEATURE_PEER_KNOWLEDGE_CONSOLIDATE,
)


IMAGE_FEATURE_KEYS: tuple[str, ...] = (
    FEATURE_IMAGE_CHAT_TOOL,
    FEATURE_IMAGE_PORTRAIT,
    FEATURE_IMAGE_FEED,
)
"""Feature keys understood by the image-profile picker (global + per-
character). Kept separate from ``GLOBAL_FEATURE_KEYS`` because image
routing uses a different value space (profile id, not provider+model)
and the UI renders them in a different panel."""


VIDEO_FEATURE_KEYS: tuple[str, ...] = (
    FEATURE_VIDEO_FEED,
)
"""Feature keys for the video-profile picker. Currently just feed; new
keys land here as we expand short-clip generation to other surfaces."""
"""Feature keys exposed by the global per-feature picker.

Includes ``chat`` so Admin owns the player-visible chat model after the
player-side model picker was removed. ``active_model`` remains the final
fallback for features that are not explicitly pinned."""


CHARACTER_FEATURE_KEYS: tuple[str, ...] = GLOBAL_FEATURE_KEYS
"""Feature keys exposed by the per-character override picker.

Matches the global catalogue, including ``chat``, so a character can pin
its main reply LLM independent of the global chat route — e.g. one
character on Anthropic Sonnet while the rest of the app stays on LM
Studio."""


ALL_FEATURE_KEYS: tuple[str, ...] = CHARACTER_FEATURE_KEYS
"""Union of every feature key the system knows about. Used by the
character-aware ``ActiveLLMProviderPort`` to decide whether to honour an
override entry — anything outside this set is silently dropped."""


# Human-readable labels for the UI's feature list. Kept here so
# backend + frontend don't drift (the /preferences/feature-models
# endpoint echoes the list of known keys + labels).
FEATURE_LABELS: dict[str, str] = {
    FEATURE_CHAT: "聊天主回覆",
    FEATURE_IMAGE_RECOGNITION: "圖片識別前處理",
    FEATURE_POST_TURN: "記憶抽取 / 狀態更新",
    FEATURE_GOAL_REVIEW: "目標審核",
    FEATURE_SCHEDULE_PLAN: "每日行程規劃",
    FEATURE_ARC_PLAN: "劇情弧規劃",
    FEATURE_ARC_SEASON_DECIDE: "劇情弧：開下一季判斷",
    FEATURE_ARC_BEAT_RECHECK: "劇情弧：重複嘗試判讀",
    FEATURE_ARC_SCENE_WRITE: "劇情弧：自主演出場景",
    FEATURE_ARC_COMPLETION_MEMORY: "劇情弧：完成里程碑記憶",
    FEATURE_STORY_EXPAND: "每日劇情展開",
    FEATURE_MEMORY_CONSOLIDATE: "記憶合併",
    FEATURE_DIALOGUE_SUMMARY: "對話摘要",
    FEATURE_NSFW_SAFE_SUMMARY: "限制級訊息安全摘要",
    FEATURE_PROMPT_REWRITE: "生圖 prompt 改寫",
    FEATURE_PROMPT_MATERIAL_DIGEST: "聊天素材去風格化摘要",
    FEATURE_NOVELTY_GATE: "玩家可見回覆品質守門",
    FEATURE_REGISTER_PROFILE: "本輪語域剖析",
    FEATURE_CHARACTER_DRAFT: "創角草稿",
    FEATURE_CHARACTER_PERSONALITY_TYPE: "創角：16 型性格分析",
    FEATURE_CHARACTER_CREATION_INTAKE: "創角：關係與畫像補問",
    FEATURE_ARC_TEMPLATE_INTAKE: "劇情骨架 wizard",
    FEATURE_ARC_ADAPT: "Fusion 劇情改編骨架",
    FEATURE_ARC_CONTINUATION_DRAFT: "ArcSeries next-season draft",
    FEATURE_FEED_COMPOSE: "動態貼文撰寫",
    FEATURE_FEED_COMMENT_REPLY: "動態留言回覆",
    FEATURE_ACTIVITY_AFTERMATH: "行程情緒尾韻",
    FEATURE_IDLE_DRIFT: "久未互動的情緒漂移",
    FEATURE_BUSY_REPLY_DECIDE: "忙碌延遲：是否延後判斷",
    FEATURE_SCENE_ACCESS: "同場可抵達性判斷",
    FEATURE_BUSY_FOLLOW_UP: "忙碌延遲：補回完整回覆",
    FEATURE_SCHEDULED_PROMISE: "排程承諾：依約定時間主動發訊息",
    FEATURE_PROACTIVE_INTENTION: "主動訊息：內心動機審核",
    FEATURE_TTS_TRANSLATE: "語音翻譯（dubbing）",
    FEATURE_CARD_TRANSLATE: "角色卡翻譯",
    FEATURE_ARC_TEMPLATE_TRANSLATE: "劇本範本翻譯",
    FEATURE_STORY_SEED_TRANSLATE: "故事種子翻譯",
    FEATURE_SILLYTAVERN_NORMALIZE: "SillyTavern 卡匯入正規化",
    FEATURE_MEMOIR_LOCALIZE: "回憶錄可見文字本地化",
    FEATURE_FUSION_STORY: "融合短篇小說",
    FEATURE_FUSION_STORY_CRITIC: "融合短篇小說潤稿審稿",
    FEATURE_BRANCHING_DRAMA: "分歧劇場",
    FEATURE_BRANCHING_DRAMA_CRITIC: "分歧劇場潤稿審稿",
    FEATURE_CHAT_REPETITION_CHECK: "聊天自我重複偵測",
    FEATURE_CHAT_ASSIST: "聊天發話輔助",
    FEATURE_PERSONA_EXTRACT: "使用者畫像：聊天後抽取",
    FEATURE_PERSONA_DREAM: "使用者畫像：夜間整理",
    FEATURE_PERSONA_PROJECTION: "使用者畫像：玩家敘事投影",
    FEATURE_PERSONA_CURIOSITY: "使用者畫像：自然探索規劃",
    FEATURE_ADDRESS_PREFERENCE_OBSERVER: "語用觀察：稱呼 / 語體偏好",
    FEATURE_RELATIONSHIP_COHERENCE: "關係稱呼一致性自癒",
    FEATURE_EXPERIMENT_ANALYSIS: "A/B 實驗：手動分析報告",
    FEATURE_CHARACTER_ENCOUNTER_PLAN: "角色互動：自然碰面規劃",
    FEATURE_CHARACTER_ENCOUNTER_BEATS: "角色互動：話題節拍規劃",
    FEATURE_CHARACTER_ENCOUNTER_DIALOGUE: "角色互動：短對話",
    FEATURE_CHARACTER_ENCOUNTER_REFLECT: "角色互動：記憶反思",
    FEATURE_PEER_KNOWLEDGE_CONSOLIDATE: "角色社交知識整理",
    FEATURE_IMAGE_CHAT_TOOL: "生圖：聊天工具",
    FEATURE_IMAGE_PORTRAIT: "生圖：角色頭像",
    FEATURE_IMAGE_FEED: "生圖：動態貼文",
    FEATURE_VIDEO_FEED: "短影片：動態貼文",
}


FEATURE_GROUP_PLAYER_FACING_VOICE = "player_facing_voice"
FEATURE_GROUP_MULTIMODAL_PERCEPTION = "multimodal_perception"
FEATURE_GROUP_HIGH_REASONING_GATES = "high_reasoning_gates"
FEATURE_GROUP_CORE_STRUCTURED_MEMORY = "core_structured_memory"
FEATURE_GROUP_LIGHT_OBSERVERS = "light_observers"
FEATURE_GROUP_LONGFORM_STORY = "longform_story"
FEATURE_GROUP_CRITIC_REVIEW = "critic_review"
FEATURE_GROUP_LANGUAGE_TRANSFORM = "language_transform"


LLM_FEATURE_GROUP_KEYS: tuple[str, ...] = (
    FEATURE_GROUP_PLAYER_FACING_VOICE,
    FEATURE_GROUP_MULTIMODAL_PERCEPTION,
    FEATURE_GROUP_HIGH_REASONING_GATES,
    FEATURE_GROUP_CORE_STRUCTURED_MEMORY,
    FEATURE_GROUP_LIGHT_OBSERVERS,
    FEATURE_GROUP_LONGFORM_STORY,
    FEATURE_GROUP_CRITIC_REVIEW,
    FEATURE_GROUP_LANGUAGE_TRANSFORM,
)


FEATURE_GROUP_LABELS: dict[str, str] = {
    FEATURE_GROUP_PLAYER_FACING_VOICE: "玩家可見角色聲音",
    FEATURE_GROUP_MULTIMODAL_PERCEPTION: "多模態圖片理解",
    FEATURE_GROUP_HIGH_REASONING_GATES: "高推理判斷與安全閘門",
    FEATURE_GROUP_CORE_STRUCTURED_MEMORY: "核心結構化記憶",
    FEATURE_GROUP_LIGHT_OBSERVERS: "輕量觀察與短判斷",
    FEATURE_GROUP_LONGFORM_STORY: "長篇敘事生成",
    FEATURE_GROUP_CRITIC_REVIEW: "審稿與潤稿",
    FEATURE_GROUP_LANGUAGE_TRANSFORM: "語言轉換與本地化",
}


FEATURE_GROUP_DESCRIPTIONS: dict[str, str] = {
    FEATURE_GROUP_PLAYER_FACING_VOICE: (
        "玩家會直接看到或感受到的角色聲音，重點是角色語感、情緒連續性與文字品質。"
    ),
    FEATURE_GROUP_MULTIMODAL_PERCEPTION: (
        "把使用者上傳圖片轉成詳細文字脈絡，讓純文字聊天與創角草稿模型仍能理解圖片。"
    ),
    FEATURE_GROUP_HIGH_REASONING_GATES: (
        "錯誤會破壞節奏、同場邏輯、長期規劃或安全邊界的判斷型任務。"
    ),
    FEATURE_GROUP_CORE_STRUCTURED_MEMORY: (
        "高頻且影響長期狀態的結構化背景任務，重點是穩定 JSON 與保守抽取。"
    ),
    FEATURE_GROUP_LIGHT_OBSERVERS: (
        "短上下文、低風險觀察或小判斷，通常適合低延遲模型。"
    ),
    FEATURE_GROUP_LONGFORM_STORY: (
        "長篇創作與互動劇場主生成，重點是長上下文、敘事結構與角色一致性。"
    ),
    FEATURE_GROUP_CRITIC_REVIEW: (
        "審稿、潤稿、批判與是否需要再 polishing 的判斷，不負責主生成。"
    ),
    FEATURE_GROUP_LANGUAGE_TRANSFORM: (
        "翻譯、本地化與 prompt 改寫，重點是語言轉換與格式保持。"
    ),
}


FEATURE_GROUP_MODEL_GUIDANCE: dict[str, str] = {
    FEATURE_GROUP_PLAYER_FACING_VOICE: (
        "建議使用中高階、擅長角色互動語言與情緒連續性的聊天模型；可犧牲一點延遲換取語感穩定。"
    ),
    FEATURE_GROUP_MULTIMODAL_PERCEPTION: (
        "必須使用支援 vision / multimodal input 的模型；重點是看圖細節、OCR、場景描述與保守不臆測。"
    ),
    FEATURE_GROUP_HIGH_REASONING_GATES: (
        "建議使用高推理、低幻覺、遵循約束能力強的模型；比起成本與速度，更重視判斷品質。"
    ),
    FEATURE_GROUP_CORE_STRUCTURED_MEMORY: (
        "建議使用 JSON 穩定、抽取保守、長期一致性好的模型；不一定要最會寫作，但格式可靠性要高。"
    ),
    FEATURE_GROUP_LIGHT_OBSERVERS: (
        "建議使用低延遲、低成本的小模型；任務多為短上下文觀察，重點是快且足夠穩定。"
    ),
    FEATURE_GROUP_LONGFORM_STORY: (
        "建議使用最強的長上下文敘事模型；重視鋪陳、角色一致性、長文結構與創作耐力。"
    ),
    FEATURE_GROUP_CRITIC_REVIEW: (
        "建議使用便宜但判斷穩定的審稿模型；需要抓重複、語氣漂移與是否值得再潤稿，不負責主生成。"
    ),
    FEATURE_GROUP_LANGUAGE_TRANSFORM: (
        "建議使用翻譯與格式保持能力好的模型；通常可用較小模型，但要避免改壞專有名詞與 JSON/tag 格式。"
    ),
}


FEATURE_GROUP_MEMBERS: dict[str, tuple[str, ...]] = {
    FEATURE_GROUP_PLAYER_FACING_VOICE: (
        FEATURE_CHAT,
        FEATURE_BUSY_FOLLOW_UP,
        FEATURE_SCHEDULED_PROMISE,
        FEATURE_FEED_COMPOSE,
        FEATURE_FEED_COMMENT_REPLY,
        FEATURE_CHARACTER_DRAFT,
        FEATURE_STORY_EXPAND,
        FEATURE_ARC_SCENE_WRITE,
        FEATURE_ARC_COMPLETION_MEMORY,
        FEATURE_PERSONA_PROJECTION,
        FEATURE_CHARACTER_ENCOUNTER_DIALOGUE,
    ),
    FEATURE_GROUP_MULTIMODAL_PERCEPTION: (
        FEATURE_IMAGE_RECOGNITION,
    ),
    FEATURE_GROUP_HIGH_REASONING_GATES: (
        FEATURE_SCENE_ACCESS,
        FEATURE_PROACTIVE_INTENTION,
        FEATURE_SCHEDULE_PLAN,
        FEATURE_ARC_PLAN,
        FEATURE_ARC_SEASON_DECIDE,
        FEATURE_ARC_BEAT_RECHECK,
        FEATURE_ARC_TEMPLATE_INTAKE,
        FEATURE_ARC_ADAPT,
        FEATURE_ARC_CONTINUATION_DRAFT,
        FEATURE_CHARACTER_PERSONALITY_TYPE,
        FEATURE_CHARACTER_CREATION_INTAKE,
        FEATURE_PERSONA_DREAM,
        FEATURE_RELATIONSHIP_COHERENCE,
        FEATURE_CHARACTER_ENCOUNTER_PLAN,
        FEATURE_CHARACTER_ENCOUNTER_BEATS,
        FEATURE_EXPERIMENT_ANALYSIS,
    ),
    FEATURE_GROUP_CORE_STRUCTURED_MEMORY: (
        FEATURE_POST_TURN,
        FEATURE_PERSONA_EXTRACT,
        FEATURE_MEMORY_CONSOLIDATE,
        FEATURE_DIALOGUE_SUMMARY,
        FEATURE_GOAL_REVIEW,
        FEATURE_PERSONA_CURIOSITY,
        FEATURE_CHARACTER_ENCOUNTER_REFLECT,
        FEATURE_PEER_KNOWLEDGE_CONSOLIDATE,
    ),
    FEATURE_GROUP_LIGHT_OBSERVERS: (
        FEATURE_ACTIVITY_AFTERMATH,
        FEATURE_IDLE_DRIFT,
        FEATURE_BUSY_REPLY_DECIDE,
        FEATURE_CHAT_REPETITION_CHECK,
        FEATURE_ADDRESS_PREFERENCE_OBSERVER,
        FEATURE_CHAT_ASSIST,
        FEATURE_PROMPT_MATERIAL_DIGEST,
        FEATURE_REGISTER_PROFILE,
        FEATURE_SILLYTAVERN_NORMALIZE,
    ),
    FEATURE_GROUP_LONGFORM_STORY: (
        FEATURE_FUSION_STORY,
        FEATURE_BRANCHING_DRAMA,
    ),
    FEATURE_GROUP_CRITIC_REVIEW: (
        FEATURE_FUSION_STORY_CRITIC,
        FEATURE_BRANCHING_DRAMA_CRITIC,
        FEATURE_NOVELTY_GATE,
    ),
    FEATURE_GROUP_LANGUAGE_TRANSFORM: (
        FEATURE_TTS_TRANSLATE,
        FEATURE_CARD_TRANSLATE,
        FEATURE_ARC_TEMPLATE_TRANSLATE,
        FEATURE_STORY_SEED_TRANSLATE,
        FEATURE_MEMOIR_LOCALIZE,
        FEATURE_PROMPT_REWRITE,
    ),
}


FEATURE_TO_GROUP: dict[str, str] = {
    feature_key: group_key
    for group_key, feature_keys in FEATURE_GROUP_MEMBERS.items()
    for feature_key in feature_keys
}
