"""Per-locale table for deterministic, backend-composed player text.

DELIBERATELY NOT AN LLM PATH. Every string here is a fixed system
message the backend emits without a model in the loop:

* channel attachment wrappers (LINE / Telegram outbound)
* inbound attachment placeholders stored as the user turn text
* the truncation-apology bubble when a tool-call JSON is unparseable
* web-push empty-body fallbacks (kept in notification_service, which
  already had its own ``_fallback_body`` table; not migrated here to
  avoid a cross-module edit war during the shared i18n cleanup)
* character-encounter deterministic fallbacks (location / trigger reason /
  schedule activity / fake-mode lines / reflection summaries)
* schedule-memorializer episodic-memory content wrappers + weekday labels
* character-creation intake follow-up questions + suggestion chips
* presence-frame channel display names (derived from the channel enum at
  the prompt layer instead of a client-sent natural-language label)

There is no semantics for a model to reason about, so a per-locale dict
is the correct shape. Do NOT "fix" this into an LLM call — the whole
point is the path that runs *when no model output is involved*. When a
new UI locale ships, add a column to ``_TEXTS`` and a family entry to
``_LANGUAGE_FAMILIES``.

Resolution mirrors ``llm_arc_planner._synthetic_template_pack``: match
the exact BCP-47 tag first, then the language subtag family
(``en-GB`` → ``en-US``), then fall back to the ship-first ``zh-TW``.
"""

from __future__ import annotations

_FALLBACK_LANGUAGE = "zh-TW"

# Supported UI locales, each keyed by its canonical BCP-47 tag. The
# language subtag ("en", "ja", "zh") is derived for family matching.
_SUPPORTED_LANGUAGES: tuple[str, ...] = ("zh-TW", "en-US", "ja-JP")


def resolve_fallback_language(language_tag: str | None) -> str:
    """Return the supported catalog key for a BCP-47 tag.

    Exact tag → language-subtag family → ``zh-TW``. Kept public so
    callers that need the resolved key for their own per-locale switch
    (e.g. notification_service) can reuse the same rule.
    """
    tag = (language_tag or "").strip()
    if tag in _SUPPORTED_LANGUAGES:
        return tag
    subtag = tag.split("-", 1)[0].lower() if tag else ""
    if subtag:
        for known in _SUPPORTED_LANGUAGES:
            if known.split("-", 1)[0].lower() == subtag:
                return known
    return _FALLBACK_LANGUAGE


# key -> {language_tag -> template}. Templates may carry ``str.format``
# named fields; callers pass them via ``**params``. Every key MUST have
# a ``zh-TW`` entry (the guaranteed fallback); en-US / ja-JP are
# best-effort and fall back to zh-TW individually if a key is missing.
_TEXTS: dict[str, dict[str, str]] = {
    # --- chat: tool-call JSON truncated / unparseable, character bubble ---
    "chat.tool_truncated_apology": {
        "zh-TW": "（抱歉，剛剛想傳圖給你時訊息被截斷了，再說一次好嗎？）",
        "en-US": (
            "(Sorry, my message got cut off just as I was trying to send you "
            "a picture. Could you say that again?)"
        ),
        "ja-JP": (
            "（ごめんね、画像を送ろうとしたら途中でメッセージが切れちゃった。"
            "もう一度言ってくれる？）"
        ),
    },
    # --- LINE outbound: attachment URL failed LINE's requirements ---
    "channel.line.attachment_url_invalid": {
        "zh-TW": "（附件 URL 不符 LINE 要求）{label}",
        "en-US": "(Attachment URL does not meet LINE's requirements) {label}",
        "ja-JP": "（添付ファイルの URL が LINE の要件を満たしていません）{label}",
    },
    # --- LINE outbound: unsupported attachment kind → text note ---
    "channel.line.attachment_label": {
        "zh-TW": "附件：{label}",
        "en-US": "Attachment: {label}",
        "ja-JP": "添付ファイル：{label}",
    },
    # --- Telegram outbound: header for non-photo attachment list ---
    "channel.telegram.other_attachments": {
        "zh-TW": "（其他附件）",
        "en-US": "(Other attachments)",
        "ja-JP": "（その他の添付ファイル）",
    },
    # --- inbound placeholders stored as the user's message text ---
    "inbound.photo_placeholder": {
        "zh-TW": "[使用者傳來一張圖片]",
        "en-US": "[The user sent an image]",
        "ja-JP": "[ユーザーが画像を送信しました]",
    },
    "inbound.attachment_placeholder": {
        "zh-TW": "[使用者傳來一個附件]",
        "en-US": "[The user sent an attachment]",
        "ja-JP": "[ユーザーが添付ファイルを送信しました]",
    },
    # --- character encounter: deterministic fallbacks (LLM unavailable /
    #     fake provider) for the plan location / trigger reason and the
    #     schedule-activity + reflection summaries that reach the player
    #     via CharacterRelationshipsPanel + MemoryBrowserPanel. ---
    "encounter.default_location": {
        "zh-TW": "日常路線上",
        "en-US": "along their usual route",
        "ja-JP": "いつもの道すがら",
    },
    "encounter.default_trigger_reason": {
        "zh-TW": "雙方行程自然交會",
        "en-US": "their schedules naturally crossed",
        "ja-JP": "お互いの予定が自然に重なった",
    },
    "encounter.fake_reason_planned": {
        "zh-TW": "雙方行程自然交會，適合短暫寒暄。",
        "en-US": "Their schedules naturally crossed — a good moment for a brief chat.",
        "ja-JP": "お互いの予定が自然に重なり、軽く言葉を交わすのにちょうどいい。",
    },
    "encounter.schedule_activity": {
        "zh-TW": "與{name}短暫碰面",
        "en-US": "A brief meetup with {name}",
        "ja-JP": "{name}とちょっとした顔合わせ",
    },
    "encounter.fake_line": {
        "zh-TW": "{speaker}在{location}自然地寒暄了一句。",
        "en-US": "{speaker} exchanged a casual greeting at {location}.",
        "ja-JP": "{speaker}は{location}で自然にひとこと挨拶を交わした。",
    },
    "encounter.summary_met": {
        "zh-TW": "在{location}和{name}短暫碰面。",
        "en-US": "Had a brief meetup with {name} at {location}.",
        "ja-JP": "{location}で{name}とちょっと顔を合わせた。",
    },
    "encounter.summary_met_short": {
        "zh-TW": "在{location}和{name}碰面。",
        "en-US": "Met {name} at {location}.",
        "ja-JP": "{location}で{name}と会った。",
    },
    "encounter.peer_fact_seen_here": {
        "zh-TW": "{name}最近會出現在{location}。",
        "en-US": "{name} has been showing up at {location} lately.",
        "ja-JP": "{name}は最近{location}に姿を見せている。",
    },
    "encounter.line_placeholder": {
        "zh-TW": "嗯，我剛好也在這裡。",
        "en-US": "Oh, I happen to be here too.",
        "ja-JP": "あ、私もちょうどここにいたんだ。",
    },
    # --- schedule memorializer: episodic memory content wrappers.
    #     These compose the player-visible memory shown in
    #     MemoryBrowserPanel and fed back into recall prompts, so they
    #     must follow the operator's content language (plan #14). ---
    "memory.schedule_location_prefix": {
        "zh-TW": "在{location}",
        "en-US": "at {location}, ",
        "ja-JP": "{location}で",
    },
    "memory.schedule_companions": {
        "zh-TW": "，和{names}一起",
        "en-US": " with {names}",
        "ja-JP": "、{names}と一緒に",
    },
    "memory.schedule_content": {
        "zh-TW": "{body}（{date} {weekday} {time_range}）",
        "en-US": "{body} ({date} {weekday} {time_range})",
        "ja-JP": "{body}（{date} {weekday} {time_range}）",
    },
    "memory.schedule_residue": {
        "zh-TW": "{content}（情緒尾韻：{residue}）",
        "en-US": "{content} (emotional residue: {residue})",
        "ja-JP": "{content}（感情の余韻：{residue}）",
    },
    # --- feed reaction memorializer: episodic memory content wrappers.
    #     Rendered into MemoryBrowserPanel + fed back into recall prompts,
    #     so they must follow the operator's content language (plan #14). ---
    "memory.feed_reaction_post_reference": {
        "zh-TW": "使用者在動態牆對你「{excerpt}」這篇貼文",
        "en-US": 'The user interacted with your feed post "{excerpt}"',
        "ja-JP": "ユーザーがあなたの投稿「{excerpt}」に反応した",
    },
    "memory.feed_reaction_liked": {
        "zh-TW": "按了讚（{count}）",
        "en-US": "liked it ({count})",
        "ja-JP": "いいねした（{count}）",
    },
    "memory.feed_reaction_commented": {
        "zh-TW": "留言：{previews}{extra}",
        "en-US": "commented: {previews}{extra}",
        "ja-JP": "コメント：{previews}{extra}",
    },
    "memory.feed_reaction_comment_extra_count": {
        "zh-TW": "（共 {count} 則）",
        "en-US": " ({count} total)",
        "ja-JP": "（計 {count} 件）",
    },
    "memory.feed_reaction_part_join": {
        "zh-TW": "，",
        "en-US": "; ",
        "ja-JP": "、",
    },
    "memory.feed_reaction_preview_join": {
        "zh-TW": "、",
        "en-US": ", ",
        "ja-JP": "、",
    },
    # --- feed composer: self-authored post memorialisation wrapper. ---
    "memory.feed_self_post": {
        "zh-TW": "我在動態牆發了一篇貼文：「{snippet}」",
        "en-US": 'I posted on the feed: "{snippet}"',
        "ja-JP": "タイムラインに投稿した：「{snippet}」",
    },
    # --- relationship milestone: interaction-volume band crossing memory.
    #     Reaches the chat prompt via relationship_anchor_block, so the
    #     interaction-heat narration must follow the operator's content
    #     language (plan #14). ---
    "milestone.band.stranger": {
        "zh-TW": "互動還很少",
        "en-US": "we've barely interacted",
        "ja-JP": "まだあまりやり取りがない",
    },
    "milestone.band.acquaintance": {
        "zh-TW": "互動漸多",
        "en-US": "we're interacting more",
        "ja-JP": "やり取りが増えてきた",
    },
    "milestone.band.familiar": {
        "zh-TW": "互動頻繁",
        "en-US": "we interact often",
        "ja-JP": "頻繁にやり取りしている",
    },
    "milestone.band.close": {
        "zh-TW": "互動很密切",
        "en-US": "we interact very closely",
        "ja-JP": "とても密にやり取りしている",
    },
    "milestone.first_crossing": {
        "zh-TW": (
            "我跟使用者的互動熱度進入「{current_label}」；"
            "這是兩人聊天量累積出的第一個互動里程碑，"
            "後續語氣可以自然反映互動變多，但不可覆蓋起始關係設定。"
        ),
        "en-US": (
            'My interaction heat with the user has reached "{current_label}"; '
            "this is the first interaction milestone built up from how much "
            "we've been chatting. My tone can naturally reflect that we "
            "interact more now, but this must not override the starting "
            "relationship setup."
        ),
        "ja-JP": (
            "ユーザーとの交流熱量が「{current_label}」に入った。"
            "これは二人の会話量が積み重なって生まれた最初の交流の節目で、"
            "以降の口調は自然に交流が増えたことを反映してよいが、"
            "最初の関係設定を上書きしてはいけない。"
        ),
    },
    "milestone.band_upgrade": {
        "zh-TW": (
            "我跟使用者的互動熱度從「{prev_label}」走到「{current_label}」了；"
            "這是聊天量自然累積出的轉折，"
            "請在不刻意提起的前提下，讓語氣與互動方式自然反映互動變化。"
        ),
        "en-US": (
            'My interaction heat with the user has moved from "{prev_label}" '
            'to "{current_label}"; this is a natural turn built up from how '
            "much we've been chatting. Without calling it out on purpose, "
            "let my tone and how I interact naturally reflect that change."
        ),
        "ja-JP": (
            "ユーザーとの交流熱量が「{prev_label}」から「{current_label}」に"
            "変わった。これは会話量が自然に積み重なって生まれた転機なので、"
            "わざわざ話題にせず、口調や関わり方が自然にその変化を"
            "反映するようにする。"
        ),
    },
    # --- arc completion memory writer: deterministic LLM-free fallback
    #     used both by NullArcCompletionMemoryWriter directly and by
    #     LLMArcCompletionMemoryWriter when the LLM call/parse fails. ---
    "memory.arc_completion_fallback": {
        "zh-TW": "我們一起走完了《{title}》：{summary}",
        "en-US": 'We finished "{title}" together: {summary}',
        "ja-JP": "私たちは一緒に『{title}』を歩みきった：{summary}",
    },
    # --- character creation intake: deterministic follow-up questions +
    #     suggestion chips shown when the LLM is unavailable (plan #9/#10).
    #     Rendered verbatim by InitialRelationshipWizardModal. ---
    "intake.q.known_context": {
        "zh-TW": "你希望她知道你們是怎麼認識的，或目前只知道到什麼程度？",
        "en-US": "How much do you want her to know about how you two met, or where you stand right now?",
        "ja-JP": "二人がどう知り合ったか、あるいは今どこまでの関係か、彼女にどこまで知っていてほしい？",
    },
    "intake.s.known_context.first_meeting": {
        "zh-TW": "第一次見面，還沒有共同背景",
        "en-US": "First time meeting, no shared history yet",
        "ja-JP": "初対面で、まだ共通の背景はない",
    },
    "intake.s.known_context.already_known": {
        "zh-TW": "已經認識，但不要補共同回憶",
        "en-US": "Already acquainted, but don't invent shared memories",
        "ja-JP": "すでに知り合いだが、共通の思い出は作らないで",
    },
    "intake.q.living_arrangement": {
        "zh-TW": "她平常跟你住在一起，還是有自己的地方？",
        "en-US": "Does she usually live with you, or does she have her own place?",
        "ja-JP": "彼女は普段あなたと一緒に暮らしている？それとも自分の場所がある？",
    },
    "intake.s.living_arrangement.together": {
        "zh-TW": "住在一起",
        "en-US": "Live together",
        "ja-JP": "一緒に住んでいる",
    },
    "intake.s.living_arrangement.nearby": {
        "zh-TW": "住附近",
        "en-US": "Live nearby",
        "ja-JP": "近くに住んでいる",
    },
    "intake.s.living_arrangement.apart": {
        "zh-TW": "分開住",
        "en-US": "Live separately",
        "ja-JP": "別々に住んでいる",
    },
    "intake.q.proactive_cadence_hint": {
        "zh-TW": "你希望她主動找你的頻率或時機大概是什麼？",
        "en-US": "How often, or at what moments, would you like her to reach out to you?",
        "ja-JP": "彼女があなたに連絡してくる頻度やタイミングはどのくらいがいい？",
    },
    "intake.s.proactive_cadence_hint.once_a_day": {
        "zh-TW": "一天最多一次",
        "en-US": "At most once a day",
        "ja-JP": "多くても一日一回",
    },
    "intake.s.proactive_cadence_hint.only_important": {
        "zh-TW": "只有想到重要事情時",
        "en-US": "Only when something important comes to mind",
        "ja-JP": "大事なことを思いついたときだけ",
    },
    "intake.s.proactive_cadence_hint.wait_for_me": {
        "zh-TW": "等我先開口",
        "en-US": "Wait for me to speak first",
        "ja-JP": "私が先に話しかけるのを待って",
    },
    "intake.q.familiarity_boundary": {
        "zh-TW": "她把你放進日常時，有沒有不要跨過的界線？",
        "en-US": "When she folds you into her day, are there any lines she shouldn't cross?",
        "ja-JP": "彼女があなたを日常に組み込むとき、越えてほしくない一線はある？",
    },
    "intake.s.familiarity_boundary.topics_only": {
        "zh-TW": "只準備話題，不安排見面",
        "en-US": "Only prepare topics, don't arrange meetups",
        "ja-JP": "話題を用意するだけで、会う約束はしない",
    },
    "intake.s.familiarity_boundary.invite_not_assume": {
        "zh-TW": "可以邀請，但不能當成已約好",
        "en-US": "May invite, but not treat it as already agreed",
        "ja-JP": "誘ってもいいが、約束済みとして扱わない",
    },
    "intake.warning.personality_type_conflict": {
        "zh-TW": "16 型性格與角色設定可能不一致，需要確認。",
        "en-US": "The 16-type personality may not match the character setup; please confirm.",
        "ja-JP": "16タイプの性格とキャラクター設定が一致しない可能性があります。ご確認ください。",
    },
    # --- weather prompt fact: label fallback when the operator has no
    #     location_label / country_code set. Reaches the weather prompt
    #     fact layer via weather_location_from_operator, so it must
    #     follow the operator's content language rather than a
    #     hardcoded Chinese literal. ---
    "weather.current_location_label": {
        "zh-TW": "目前位置",
        "en-US": "Current location",
        "ja-JP": "現在地",
    },
    # --- presence frame: channel display name derived at the prompt
    #     layer from the channel enum (plan #1 / D4). The client no longer
    #     sends a natural-language display_name; this label is what the
    #     LLM sees for "current interface", so it must follow the
    #     operator's content language. Keyed by ChatChannel.value. ---
    "presence.channel.kokoro_stage": {
        "zh-TW": "站內同場互動",
        "en-US": "in-app shared presence",
        "ja-JP": "アプリ内の同席",
    },
    "presence.channel.kokoro_dm": {
        "zh-TW": "站內私訊",
        "en-US": "in-app direct message",
        "ja-JP": "アプリ内のダイレクトメッセージ",
    },
    "presence.channel.telegram": {
        "zh-TW": "Telegram",
        "en-US": "Telegram",
        "ja-JP": "Telegram",
    },
    "presence.channel.line": {
        "zh-TW": "LINE",
        "en-US": "LINE",
        "ja-JP": "LINE",
    },
    "presence.channel.unknown": {
        "zh-TW": "外部訊息",
        "en-US": "external message",
        "ja-JP": "外部メッセージ",
    },
}

# Weekday labels indexed by ``date.weekday()`` (0 = Monday). Kept
# separate from ``_TEXTS`` because it is a positional list, not a
# ``str.format`` template. Player-visible via memorialized schedule
# memories, so it must localise off the operator's content language.
_WEEKDAY_LABELS: dict[str, tuple[str, ...]] = {
    "zh-TW": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"),
    "en-US": (
        "Monday", "Tuesday", "Wednesday", "Thursday",
        "Friday", "Saturday", "Sunday",
    ),
    "ja-JP": ("月曜日", "火曜日", "水曜日", "木曜日", "金曜日", "土曜日", "日曜日"),
}


def localized_weekday_label(weekday_index: int, language_tag: str | None) -> str:
    """Return the operator-language weekday name for ``date.weekday()``.

    Falls back to the ship-first ``zh-TW`` table when the tag is
    unknown, mirroring ``localized_fallback_text``.
    """
    language = resolve_fallback_language(language_tag)
    labels = _WEEKDAY_LABELS.get(language) or _WEEKDAY_LABELS[_FALLBACK_LANGUAGE]
    return labels[weekday_index % 7]


def localized_fallback_text(
    key: str,
    language_tag: str | None,
    /,
    **params: object,
) -> str:
    """Look up a deterministic player-visible string for ``key``.

    Picks the operator's language (exact → family → zh-TW), then formats
    any ``{named}`` fields from ``params``. Unknown keys raise ``KeyError``
    — every caller uses a compile-time-known key, so a typo should fail
    loud in tests rather than silently ship blank text.
    """
    catalog = _TEXTS[key]
    language = resolve_fallback_language(language_tag)
    template = catalog.get(language) or catalog[_FALLBACK_LANGUAGE]
    if params:
        return template.format(**params)
    return template
