"""Unit tests for the neutral structured storyboard (CV3-B / plan D12).

This module is the **producer** half of a contract whose consumer raises:
``media-worker``'s ``h3_prompt.render_storyboard_prompt`` throws on a shot
with no cut time, a cut that runs backwards, or an empty storyboard — and
a throw there fails the whole video job. So the rules pinned here are not
stylistic:

* the first shot never carries a cut time;
* every later shot carries a strictly increasing one, below the clip
  length;
* a shot that cannot satisfy that is **dropped**, not forwarded;
* the serialised form is always valid JSON — never a truncated document,
  which the worker would mistake for prose and render into the clip.

Plus the two things D12 added to the brief: dialogue stays in the
character's own language, and the audio blocks survive the round trip.
"""

from __future__ import annotations

import json

from kokoro_link.application.services.video_storyboard_shape import (
    MAX_STORYBOARD_CHARS,
    NeutralStoryboard,
    StoryboardShot,
    looks_like_schema_leak,
    parse_storyboard,
)

_FULL = {
    "shots": [
        {
            "visual": "A young woman sits by a rain-streaked cafe window.",
            "style": "2D-animated, cinematic",
            "consistency_anchors": [
                "black hair in a low ponytail",
                "grey hoodie",
                "seated at frame left",
                "the cafe window behind her",
            ],
            "camera": {
                "motion_type": "Push In",
                "amplitude": "small",
                "speed": "slow",
            },
        },
        {
            "visual": "She turns her head slowly toward the glass.",
            "start_at_seconds": 2.5,
            "camera": {"motion_type": "Static Shot"},
        },
    ],
    "overall_soundscape": "Steady rain against the glass, muffled chatter.",
    "non_diegetic_music": "Sparse solo piano, slow tempo.",
}


def _raw(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


# -- the happy shape ---------------------------------------------------


def test_parses_the_full_neutral_shape() -> None:
    storyboard = parse_storyboard(_raw(_FULL), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 2
    first, second = storyboard.shots
    assert first.style == "2D-animated, cinematic"
    assert first.consistency_anchors[0] == "black hair in a low ponytail"
    assert first.camera is not None
    assert first.camera.motion_type == "Push In"
    assert second.start_at_seconds == 2.5
    assert storyboard.soundscape.startswith("Steady rain")
    assert storyboard.music.startswith("Sparse solo piano")


def test_round_trips_through_json_into_the_worker_shape() -> None:
    """The wire form is a JSON string with a top-level ``shots`` array —
    that is exactly what ``h3_prompt.coerce_storyboard`` sniffs for."""
    storyboard = parse_storyboard(_raw(_FULL), clip_seconds=5)
    assert storyboard is not None

    decoded = json.loads(storyboard.to_json())

    assert isinstance(decoded["shots"], list)
    assert decoded["overall_soundscape"]
    assert decoded["non_diegetic_music"]


def test_parses_fenced_and_prose_wrapped_answers() -> None:
    fenced = f"```json\n{_raw(_FULL)}\n```"
    chatty = f"Sure!\n{_raw(_FULL)}\nHope that helps."

    assert parse_storyboard(fenced, clip_seconds=5) is not None
    assert parse_storyboard(chatty, clip_seconds=5) is not None


# -- cut times: the rules the worker raises on -------------------------


def test_first_shot_never_carries_a_cut_time() -> None:
    """The guide forbids a timestamp on the opening shot, and a model that
    dutifully writes ``0`` there must not be able to leak it."""
    payload = json.loads(_raw(_FULL))
    payload["shots"][0]["start_at_seconds"] = 0

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert storyboard.shots[0].start_at_seconds is None
    assert "start_at_seconds" not in json.loads(storyboard.to_json())["shots"][0]


def test_a_later_shot_without_a_cut_time_is_dropped() -> None:
    payload = {
        "shots": [
            {"visual": "She sits still."},
            {"visual": "She looks up."},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 1


def test_cut_times_that_do_not_increase_are_dropped() -> None:
    payload = {
        "shots": [
            {"visual": "She sits still."},
            {"visual": "She looks up.", "start_at_seconds": 3},
            {"visual": "She looks down again.", "start_at_seconds": 2},
            {"visual": "She smiles.", "start_at_seconds": 4},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert [shot.start_at_seconds for shot in storyboard.shots] == [
        None, 3.0, 4.0,
    ]


def test_a_cut_at_or_past_the_end_of_the_clip_is_dropped() -> None:
    """The worker silently discards these; dropping them here keeps the
    payload honest about what will actually be rendered."""
    payload = {
        "shots": [
            {"visual": "She sits still."},
            {"visual": "She stands up.", "start_at_seconds": 5},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 1


def test_numeric_string_cut_times_are_accepted() -> None:
    payload = {
        "shots": [
            {"visual": "She sits still."},
            {"visual": "She looks up.", "start_at_seconds": "2.5s"},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert storyboard.shots[1].start_at_seconds == 2.5


def test_a_boolean_cut_time_is_not_read_as_one_second() -> None:
    """``bool`` is an ``int`` subclass — without the explicit guard,
    ``start_at_seconds: true`` would invent a cut at 1.0s."""
    payload = {
        "shots": [
            {"visual": "She sits still."},
            {"visual": "She looks up.", "start_at_seconds": True},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 1


# -- missing / malformed fields ----------------------------------------


def test_a_bare_visual_is_enough() -> None:
    storyboard = parse_storyboard(
        _raw({"shots": [{"visual": "She breathes slowly."}]}), clip_seconds=5,
    )

    assert storyboard is not None
    assert storyboard.shots[0].camera is None
    assert storyboard.shots[0].consistency_anchors == ()
    assert storyboard.soundscape == ""
    # An absent soundscape must not become an empty key: the worker fills
    # it with a neutral ambience, and ``N/A`` there would ship silence.
    assert "overall_soundscape" not in json.loads(storyboard.to_json())


def test_shots_with_neither_visual_nor_dialogue_are_dropped() -> None:
    payload = {
        "shots": [
            {"style": "2D-animated"},
            {"visual": "She breathes slowly."},
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 1
    # The survivor became shot 1, so it must not have inherited a cut time.
    assert storyboard.shots[0].start_at_seconds is None


def test_an_empty_shots_array_is_not_a_storyboard() -> None:
    assert parse_storyboard(_raw({"shots": []}), clip_seconds=5) is None


def test_an_object_without_shots_is_not_a_storyboard() -> None:
    raw = _raw({"storyboard_prompt": "slow push-in over five seconds"})
    assert parse_storyboard(raw, clip_seconds=5) is None


def test_plain_prose_is_not_a_storyboard() -> None:
    assert parse_storyboard("Starts on the given frame.", clip_seconds=5) is None
    assert parse_storyboard("", clip_seconds=5) is None


def test_camera_vocabulary_is_canonicalised_not_invented() -> None:
    payload = {
        "shots": [
            {
                "visual": "She breathes slowly.",
                "camera": {
                    "motion_type": "truck_right",
                    "amplitude": "medium",
                    "speed": "normal",
                },
            },
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    camera = storyboard.shots[0].camera
    assert camera is not None
    assert camera.motion_type == "Truck Right"
    # medium / normal are expressed by *omission*, per the guide.
    assert camera.amplitude == ""
    assert camera.speed == ""


def test_an_unknown_camera_move_survives_verbatim() -> None:
    """The adapter renders unknown moves grammatically; dropping the only
    camera direction the storyboard had would be the worse failure."""
    payload = {
        "shots": [
            {
                "visual": "She breathes slowly.",
                "camera": {"motion_type": "crash zoom"},
            },
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert storyboard.shots[0].camera is not None
    assert storyboard.shots[0].camera.motion_type == "crash zoom"


def test_style_and_anchors_are_kept_only_on_the_opening_shot() -> None:
    payload = {
        "shots": [
            {"visual": "She sits still.", "style": "2D-animated"},
            {
                "visual": "She looks up.",
                "start_at_seconds": 2,
                "style": "Live-action",
                "consistency_anchors": ["something else"],
            },
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert storyboard.shots[0].style == "2D-animated"
    assert storyboard.shots[1].style == ""
    assert storyboard.shots[1].consistency_anchors == ()


# -- dialogue ----------------------------------------------------------


def test_dialogue_keeps_the_character_s_own_language() -> None:
    payload = {
        "shots": [
            {
                "visual": "She looks at the rain.",
                "dialogue": [
                    {
                        "speaker_id": "S1",
                        "text": "雨還沒停呢。",
                        "language": "Chinese",
                        "speaker_description": "the woman in the grey hoodie",
                    },
                ],
            },
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    line = storyboard.shots[0].dialogue[0]
    assert line.text == "雨還沒停呢。"
    assert line.language == "Chinese"
    # And it survives serialisation unescaped-by-value.
    decoded = json.loads(storyboard.to_json())
    assert decoded["shots"][0]["dialogue"][0]["text"] == "雨還沒停呢。"


def test_a_dialogue_entry_without_text_is_dropped_not_fatal() -> None:
    """``h3_prompt._dialogue_from_mapping`` raises on a textless entry, so
    it must never reach the wire."""
    payload = {
        "shots": [
            {
                "visual": "She looks at the rain.",
                "dialogue": [{"speaker_id": "S1"}, {"text": "嗯。"}],
            },
        ],
    }

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots[0].dialogue) == 1
    assert storyboard.shots[0].dialogue[0].text == "嗯。"


def test_a_shot_that_is_only_dialogue_still_counts() -> None:
    payload = {"shots": [{"dialogue": [{"text": "嗯。", "language": "Chinese"}]}]}

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert storyboard.shots[0].visual == ""


# -- salvage (the AE-series 4096-ceiling lesson) ------------------------


def test_salvages_complete_shots_out_of_a_truncated_answer() -> None:
    raw = _raw(_FULL)
    # Cut just after the shots array closes: both shots are intact, the
    # audio blocks never made it, and the document has no closing brace —
    # so neither ``json.loads`` nor a ``{...}`` block regex can recover it.
    truncated = raw[: raw.index('"overall_soundscape"')]

    storyboard = parse_storyboard(truncated, clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 2
    assert storyboard.shots[1].start_at_seconds == 2.5
    assert storyboard.soundscape == ""


def test_salvage_keeps_the_audio_blocks_when_the_cut_came_later() -> None:
    raw = _raw(_FULL)
    truncated = raw[: raw.rindex('"non_diegetic_music"') - 2] + "..."

    storyboard = parse_storyboard(truncated, clip_seconds=5)

    assert storyboard is not None
    assert storyboard.soundscape.startswith("Steady rain")


def test_a_shot_cut_mid_object_is_not_half_salvaged() -> None:
    raw = '{"shots":[{"visual":"She sits still."},{"visual":"She loo'

    storyboard = parse_storyboard(raw, clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots) == 1


def test_a_cut_that_left_no_complete_shot_yields_nothing() -> None:
    assert parse_storyboard('{"shots":[{"visual":"She si', clip_seconds=5) is None


# -- size ceiling ------------------------------------------------------


def test_overflow_drops_trailing_shots_rather_than_cutting_the_string() -> None:
    """A truncated JSON document still starts with ``{``, so the worker
    would take it for prose and render the envelope into the clip."""
    long_visual = "She breathes slowly by the window. " * 40
    storyboard = NeutralStoryboard(
        shots=(
            StoryboardShot(visual=long_visual),
            StoryboardShot(visual=long_visual, start_at_seconds=2.0),
            StoryboardShot(visual=long_visual, start_at_seconds=3.0),
        ),
        soundscape="Steady rain against the glass.",
    )

    text = storyboard.to_json(max_chars=MAX_STORYBOARD_CHARS)

    assert len(text) <= MAX_STORYBOARD_CHARS
    decoded = json.loads(text)  # must still be valid JSON
    assert len(decoded["shots"]) < 3
    assert decoded["overall_soundscape"]


def test_one_oversized_shot_is_clamped_rather_than_dropped() -> None:
    storyboard = NeutralStoryboard(
        shots=(StoryboardShot(visual="word " * 2000),),
    )

    text = storyboard.to_json(max_chars=400)

    assert len(text) <= 400
    assert json.loads(text)["shots"][0]["visual"]


def test_a_runaway_field_is_clamped_at_parse_time() -> None:
    payload = {"shots": [{"visual": "x" * 5000}], "overall_soundscape": "y" * 5000}

    storyboard = parse_storyboard(_raw(payload), clip_seconds=5)

    assert storyboard is not None
    assert len(storyboard.shots[0].visual) < 5000
    assert len(storyboard.soundscape) < 5000


# -- schema leak -------------------------------------------------------


def test_a_half_serialised_envelope_is_recognised_as_a_leak() -> None:
    assert looks_like_schema_leak('{"shots": [{"visual": "cut off mid str')
    assert looks_like_schema_leak('{"storyboard_prompt": "cut off')
    assert not looks_like_schema_leak("Starts exactly on the given frame.")
