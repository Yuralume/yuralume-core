"""FX1: the four defects the DH/L2 review found in the migrated layer.

Each section pins one finding with the exact input it was found on, so
that a future edit that reverts the fix fails here rather than in
production. The differential corpus proves "nothing was lost"; this file
proves "these specific things were gained, and this one thing was
deliberately given up".

- **DH-1** — deep unclosed nesting made ``json.loads`` raise
  ``RecursionError``, which is not a ``JSONDecodeError`` and so escaped
  every guard in the chain and killed the turn.
- **DH-2** — the array guard at nine object-reading sites was spelled as
  "the whole reply parses and isn't a dict", which stops being true the
  moment a model writes one sentence after its array.
- **DH-3** — five translator sites turned truncation repair on during
  migration, which persists half-sentences over correct source prose.
- **L2-1 / L2-3** — anchoring on the first ``{`` reads the wrong object
  when a reasoning-first model writes a thought object before the
  envelope.
- **L2-4** — two sites where prose is a *designed-legal* reply warned on
  every prose reply.
"""

from __future__ import annotations

import json
import logging

import pytest

from kokoro_link.application.services import arc_template_intake_service as arc_svc
from kokoro_link.application.services import branching_drama_critic as bdc_svc
from kokoro_link.application.services import branching_drama_director as bdd_svc
from kokoro_link.application.services import branching_drama_planner as bdp_svc
from kokoro_link.application.services import (
    character_creation_intake_service as cci_svc,
)
from kokoro_link.application.services import character_encounter_service as ces_svc
from kokoro_link.application.services import fusion_story_critic as fsc_svc
from kokoro_link.application.services import fusion_story_planner as fsp_svc
from kokoro_link.application.services import video_storyboard_shape as vss_svc
from kokoro_link.application.services.showcase import json_output as jo_svc
from kokoro_link.application.services.tool_call_parser import parse_tool_call
from kokoro_link.infrastructure.character_card import llm_translator as cc_translator
from kokoro_link.infrastructure.character_card import (
    sillytavern_normalizer as st_normalizer,
)
from kokoro_link.infrastructure.feed.llm_composer import (
    _InvalidComposerOutput,
    _parse_output,
)
from kokoro_link.infrastructure.memoir import llm_localizer as memoir_localizer
from kokoro_link.infrastructure.memory.json_parser import parse_memory_payload
from kokoro_link.infrastructure.social import (
    llm_peer_knowledge_consolidator as pkc_svc,
)
from kokoro_link.infrastructure.story import (
    llm_arc_template_translator as arc_translator,
)
from kokoro_link.infrastructure.story import llm_story_seed_translator as seed_translator
from kokoro_link.llm_output import (
    MAX_NESTING_DEPTH,
    extract_array,
    extract_object,
    first_balanced_region,
    first_region_is_array,
)
from tests.unit.llm_output._frozen_oracles import frozen_tool_call_object
from tests.unit.llm_output.test_differential_wave_rest import (
    frozen_group_a_parse_json_object,
)


# ======================================================================
# DH-1 — RecursionError escaping the extractors
# ======================================================================

_LOOPED_ARGUMENT = '{"tool": "web_search", "args": {"queries": ' + "[" * 2999
"""The reported input: a real tool-call envelope whose argument value
runs away into ~3000 unclosed brackets (3042 chars). The object anchor
finds a legitimate ``{``, the region never closes, and repair then hands
``json.loads`` a candidate nested three thousand deep."""


def test_a_looping_model_no_longer_takes_the_turn_down_with_it() -> None:
    """``parse_tool_call`` runs on every chat reply and its caller in
    ``chat_service`` has no ``try``/``except`` around it, so an exception
    here is a dead turn, not a degraded one."""
    assert parse_tool_call(_LOOPED_ARGUMENT) is None


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_LOOPED_ARGUMENT, id="contract_then_deep_nesting"),
        pytest.param("[" * 3000, id="unclosed_array"),
        pytest.param("{" * 3000, id="unclosed_object"),
        pytest.param("[{" * 1500, id="unclosed_mixed"),
        pytest.param("[" * 1200 + "]" * 1200, id="balanced_deep"),
    ],
)
def test_no_extractor_raises_on_runaway_nesting(raw: str) -> None:
    for repair in (True, False):
        assert extract_object(raw, repair_truncated=repair) is None
        assert extract_array(raw, repair_truncated=repair) is None
    assert parse_tool_call(raw) is None
    assert parse_memory_payload(raw) == []
    first_balanced_region(raw)
    first_region_is_array(raw)


def test_the_tool_path_crashed_on_this_before_the_migration_too() -> None:
    """Honest record, and the reason the ``pathological.`` corpus family
    is excluded from the differential comparison: the frozen
    pre-migration kernel does not merely mis-read this input, it raises.
    "New reads everything old read" has no content against a crash."""
    with pytest.raises(RecursionError):
        frozen_tool_call_object(_LOOPED_ARGUMENT)


def test_the_depth_bound_is_the_one_deliberate_narrowing() -> None:
    """``MAX_NESTING_DEPTH`` is not free: a *balanced* region deeper than
    the bound used to decode and now does not. That trade is the point —
    the deepest contract in this codebase nests four levels, and no
    payload worth reading sits past two hundred — but it is a narrowing
    and is written down as one rather than hidden behind an exclusion.
    """
    at_bound = "[" * MAX_NESTING_DEPTH + "]" * MAX_NESTING_DEPTH
    past_bound = "[" * (MAX_NESTING_DEPTH + 1) + "]" * (MAX_NESTING_DEPTH + 1)

    assert extract_array(at_bound) is not None
    assert extract_array(past_bound) is None
    # Still parseable by a plain decoder — i.e. the refusal is our bound
    # talking, not the input being broken.
    assert isinstance(json.loads(past_bound), list)


def test_real_payload_depth_is_nowhere_near_the_bound() -> None:
    """The post-turn five-in-one reply is the deepest contract we ask
    for. If this ever approaches the bound, the bound is wrong."""
    post_turn = (
        '{"memories": [{"kind": "semantic", "content": "x", "tags": ["音樂"]}], '
        '"state": {"emotion": "開心"}}'
    )
    assert extract_object(post_turn) is not None


# ======================================================================
# DH-2 — the array guard that vanished when the reply had a tail
# ======================================================================

_ARRAY_WITH_COMMENTARY = (
    '[{"summary": "第一位鄰居", "severity": 1}, '
    '{"summary": "第二位鄰居", "severity": 2}]\n'
    "以上，有需要再跟我說！"
)
"""A model asked for one object answers with a list of them and signs
off. The array closes; the *reply* does not parse."""


def test_the_old_spelling_of_the_guard_was_off_for_this_input() -> None:
    """Why the guard had to become structural: the reply is
    unmistakably array-shaped, yet a whole-string ``json.loads`` — the
    old guard's entire implementation — raises on it and therefore
    answered "not an array reply"."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(_ARRAY_WITH_COMMENTARY)
    assert first_region_is_array(_ARRAY_WITH_COMMENTARY) is True


def test_the_unguarded_extractor_still_reaches_into_the_array() -> None:
    """The guard is doing the work, not the extractor. Without it the
    call sites below receive the array's *first element* — a well-shaped
    dict that passes every ``isinstance`` check they have."""
    assert extract_object(_ARRAY_WITH_COMMENTARY) == {
        "summary": "第一位鄰居", "severity": 1,
    }


def test_peer_knowledge_no_longer_writes_peer_as_profile_onto_peer_b() -> None:
    """The site the finding was proved on. This call is about *one*
    peer; handing it the first element of a list of peers writes the
    wrong person's profile into the current peer's record, silently and
    permanently."""
    assert pkc_svc._json_object(_ARRAY_WITH_COMMENTARY) is None


def test_every_object_reading_site_refuses_an_array_shaped_reply() -> None:
    assert ces_svc._json_object(_ARRAY_WITH_COMMENTARY) is None
    assert cci_svc._extract_json_object(_ARRAY_WITH_COMMENTARY) is None
    assert vss_svc.decode_json_object(_ARRAY_WITH_COMMENTARY) is None
    assert jo_svc.coerce_json_object(_ARRAY_WITH_COMMENTARY) is None
    assert bdp_svc._decode_planner_json(_ARRAY_WITH_COMMENTARY, site="t") is None
    assert bdc_svc._parse_critique(_ARRAY_WITH_COMMENTARY, paragraph_count=3) is None
    assert fsc_svc._parse_critique(_ARRAY_WITH_COMMENTARY, paragraph_count=3) is None
    assert fsp_svc._parse_outline(_ARRAY_WITH_COMMENTARY, briefs=()) is None


def test_the_guard_still_lets_a_genuine_object_reply_through() -> None:
    """The refusal is aimed at the *shape*, not at trailing text: an
    object with the same sign-off after it must still be read."""
    raw = '{"summary": "看起來沒問題", "severity": 0}\n以上，有需要再跟我說！'
    assert pkc_svc._json_object(raw) == {"summary": "看起來沒問題", "severity": 0}
    assert ces_svc._json_object(raw) is not None
    assert jo_svc.coerce_json_object(raw) is not None


# ======================================================================
# DH-3 — translation repair persisting half-sentences
# ======================================================================

_TRUNCATED_TRANSLATION = '{"summary": "她住在淡水的老公寓，窗外能看見'
"""A translation reply chopped by ``max_tokens`` mid-sentence. Repair
closes the dangling string, so the value below is a perfectly ordinary
``str`` — nothing downstream can tell it is half a sentence."""


@pytest.mark.parametrize(
    "parse",
    [
        pytest.param(cc_translator._parse_json_object, id="character_card.translator"),
        pytest.param(
            st_normalizer._parse_json_object, id="character_card.sillytavern",
        ),
        pytest.param(memoir_localizer._parse_json_object, id="memoir.localizer"),
        pytest.param(arc_translator._parse_json_object, id="story.arc_template"),
        pytest.param(seed_translator._parse_json_object, id="story.story_seed"),
    ],
)
def test_translator_sites_refuse_a_truncated_translation(parse) -> None:
    """Fail closed, exactly as the pre-migration code did: ``{}`` means
    "keep the original text", and an untranslated sentence beats a
    translated half-sentence written over it permanently."""
    assert parse(_TRUNCATED_TRANSLATION) == {}
    # ...and the frozen pre-migration kernel agreed, which is the point:
    # turning repair on here was a silent behaviour change, not a gain.
    assert frozen_group_a_parse_json_object(_TRUNCATED_TRANSLATION) == {}


def test_repair_is_what_was_turned_off_not_the_extraction() -> None:
    """Guard against "fixed" by accident: the same input *is* repairable
    by the shared layer, so the sites above are declining it on purpose.
    """
    assert extract_object(_TRUNCATED_TRANSLATION, repair_truncated=True) is not None
    assert extract_object(_TRUNCATED_TRANSLATION, repair_truncated=False) is None


def test_translator_sites_still_read_a_complete_reply() -> None:
    complete = '{"summary": "她住在淡水的老公寓。"}'
    assert cc_translator._parse_json_object(complete) == {"summary": "她住在淡水的老公寓。"}
    assert memoir_localizer._parse_json_object(complete) == {
        "summary": "她住在淡水的老公寓。",
    }


# ======================================================================
# L2-1 / L2-3 — the first object is not always the right object
# ======================================================================

_COMPOSER_THOUGHT_FIRST = (
    '{"thought": "先想一下今天發生了什麼"}\n'
    '{"content_text": "今天在河堤散步，風有點涼。", '
    '"image_prompt": "1girl, riverside, evening"}'
)


def test_feed_composer_finds_the_post_behind_a_thought_object() -> None:
    """L2-1. Anchored on the first ``{``, ``content_text`` came back
    ``None`` and the entire post was discarded — with the finished post
    sitting in the very next region."""
    out = _parse_output(_COMPOSER_THOUGHT_FIRST, image_required=True)

    assert out.content_text == "今天在河堤散步，風有點涼。"
    assert out.image_prompt == "1girl, riverside, evening"


def test_feed_composer_still_fails_closed_on_a_wrapper_less_reply() -> None:
    """The scan looks for the schema's own key, so it cannot turn junk
    into a post. Automatic posts still have to cross the structured
    boundary."""
    with pytest.raises(_InvalidComposerOutput):
        _parse_output('{"thought": "沒什麼好說的"}', image_required=False)


def test_feed_composer_prefers_the_first_region_when_it_is_the_envelope() -> None:
    """Additive only: an envelope that already parsed is never
    redirected to some later object."""
    raw = (
        '{"content_text": "第一則", "image_prompt": "a"}\n'
        '{"content_text": "第二則", "image_prompt": "b"}'
    )
    assert _parse_output(raw, image_required=True).content_text == "第一則"


def test_director_finds_the_narration_behind_a_reasoning_object() -> None:
    """L2-3, and the reason it is worse than a plain miss: a missing
    ``response`` key comes back as ``""`` from ``.get``, which *is* a
    ``str``, so the JSON branch was taken and the player got the
    "（回應生成失敗）" placeholder rather than the narration."""
    raw = (
        '{"reasoning": "玩家想往前走"}\n'
        '{"response": "你推開門，屋裡一片安靜。", "advance_hint": "往裡走"}'
    )
    assert bdd_svc._parse_scene_response(raw) == ("你推開門，屋裡一片安靜。", "往裡走")


def test_director_degrades_to_the_raw_text_not_to_the_placeholder() -> None:
    """When no region carries ``response``, a reply that is *not* just
    an envelope falls back to raw-text-as-narration — the pre-migration
    outcome. A placeholder here would be strictly worse than the prose
    the model actually wrote."""
    raw = '{"reasoning": "玩家想往前走"}\n你推開門，屋裡一片安靜。'
    response, hint = bdd_svc._parse_scene_response(raw)

    assert response == raw.strip()
    assert hint is None


def test_director_keeps_the_placeholder_for_an_envelope_shaped_reply() -> None:
    """The other side of the same branch, pinned so the fallback above
    cannot swallow it: when the whole reply *is* one object that simply
    lacks ``response``, the placeholder is the pre-migration outcome and
    stays."""
    assert bdd_svc._parse_scene_response('{"tool": "web_search"}') == (
        "（回應生成失敗）", None,
    )


def test_director_still_repairs_a_truncated_envelope() -> None:
    truncated = '{"response": "先進去看看", "advance_hint": "踏上旅'
    assert bdd_svc._parse_scene_response(truncated) == ("先進去看看", "踏上旅")


# ======================================================================
# L2-4 — warning on a reply the design says is fine
# ======================================================================

_DIRECTOR_LOGGER = "kokoro_link.application.services.branching_drama_director"
_ARC_INTAKE_LOGGER = "kokoro_link.application.services.arc_template_intake_service"


def test_director_does_not_warn_on_a_prose_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Prose is a *designed-legal* reply on this path — the fallback
    treats it as the narration. Warning on every one of them buries the
    ``unbalanced`` / ``decode_error`` lines that do mean something."""
    with caplog.at_level(logging.DEBUG, logger=_DIRECTOR_LOGGER):
        bdd_svc._parse_scene_response("你推開門，屋裡一片安靜。")

    assert caplog.records == []


def test_director_still_warns_when_the_model_botched_the_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_DIRECTOR_LOGGER):
        bdd_svc._parse_scene_response('{"response": broken}')

    assert any(
        "branching_drama.director.scene_response" in record.getMessage()
        for record in caplog.records
    )


def test_arc_template_intake_does_not_warn_on_a_prose_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_parse_beat_summary_response`` documents prose as a legal
    degradation — the whole response becomes the summary. So ``no_json``
    here is a sanctioned outcome, not a failure."""
    with caplog.at_level(logging.DEBUG, logger=_ARC_INTAKE_LOGGER):
        arc_svc._extract_json_object("這一段的重點是她終於說出口了。")

    assert caplog.records == []


def test_arc_template_intake_still_warns_on_a_botched_envelope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger=_ARC_INTAKE_LOGGER):
        arc_svc._extract_json_object('{"summary": broken}')

    assert any(
        "arc_template_intake.json" in record.getMessage()
        for record in caplog.records
    )
