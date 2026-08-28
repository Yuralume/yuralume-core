"""TR1: frame-based ``world_awareness_enabled`` creation default.

Pure-function unit tests for
``kokoro_link.domain.value_objects.world_frame.default_world_awareness_enabled``.
Service/entity-level wiring is covered in ``test_character_service.py``.
"""

import pytest

from kokoro_link.domain.value_objects.world_frame import (
    default_world_awareness_enabled,
)


@pytest.mark.parametrize(
    "frame,expected",
    [
        ("modern", True),
        ("Modern", True),
        ("  modern  ", True),
        ("any", True),
        ("fantasy", False),
        ("school", False),
        ("custom", False),
        ("period", False),
        ("", False),
        ("   ", False),
    ],
)
def test_known_and_blank_frames(frame: str, expected: bool) -> None:
    assert default_world_awareness_enabled(frame) is expected


@pytest.mark.parametrize("frame", ["steampunk", "cyberpunk-2099", "未知世界"])
def test_unrecognised_frame_is_conservative(frame: str) -> None:
    # Unknown values never get keyword-guessed into "modern" — they fall
    # back to the same off default as fantasy/school/custom.
    assert default_world_awareness_enabled(frame) is False


@pytest.mark.parametrize("value", [None, 123, [], {}])
def test_non_string_input_is_conservative(value: object) -> None:
    assert default_world_awareness_enabled(value) is False  # type: ignore[arg-type]
