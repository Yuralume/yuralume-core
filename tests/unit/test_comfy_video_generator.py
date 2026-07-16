"""BDD for ``ComfyVideoGenerator`` output parsing.

The Wan2.2 reference workflow uses a SaveVideo node that lists its
mp4 entry under the ``images`` output key with a sibling
``animated: [true]`` flag — *not* under ``videos`` / ``gifs`` like
other ComfyUI video nodes. An earlier adapter only walked
``videos`` / ``gifs`` and silently raised ``VideoNoOutputError`` on
every Wan2.2 run, which made the feed composer fall back to image
posts even though generation succeeded.

These tests pin the file-extension-based output detection so a
future refactor doesn't regress that behavior.
"""

from __future__ import annotations

import pytest

from kokoro_link.contracts.video_provider import VideoNoOutputError
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.comfyui.video_generator import (
    ComfyVideoGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.wan_video_workflow import (
    WanVideoWorkflowBuilder,
)


class _FakeClient:
    def __init__(self, history_outputs: dict) -> None:
        self._history_outputs = history_outputs
        self.queued_prompts: list[dict] = []
        self.downloaded: list[tuple[str, str, str]] = []

    async def queue_prompt(self, prompt: dict) -> str:
        self.queued_prompts.append(prompt)
        return "pid-1"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {"outputs": self._history_outputs}

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        self.downloaded.append((filename, subfolder, folder_type))
        return b"FAKE_MP4_BYTES"


def _character() -> Character:
    return Character.create(
        name="Probe",
        summary="",
        personality=[], interests=[], speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="", affection=0, fatigue=0, trust=0, energy=100,
        ),
        appearance="short dark hair",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous traveler",
    )


def _generator(client: _FakeClient) -> ComfyVideoGenerator:
    return ComfyVideoGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WanVideoWorkflowBuilder(),
        fps=16,
        default_length_frames=81,
        default_width=480,
        default_height=832,
    )


@pytest.mark.asyncio
async def test_picks_up_mp4_listed_under_images_key() -> None:
    # The wild SaveVideo node emits mp4 file under ``images`` and
    # signals it with ``animated: [true]`` — must still be recognized.
    client = _FakeClient(history_outputs={
        "31": {
            "images": [
                {"filename": "clip_00001_.mp4", "subfolder": "kokoro/feed", "type": "output"},
            ],
            "animated": [True],
        },
    })
    blob = await _generator(client).generate(
        character=_character(), positive="a girl tilts her head",
    )
    assert blob == b"FAKE_MP4_BYTES"
    assert client.downloaded == [("clip_00001_.mp4", "kokoro/feed", "output")]
    queued_text = str(client.queued_prompts[0])
    assert "Character gender identity: 非二元" in queued_text
    assert "Visual gender presentation: androgynous traveler" in queued_text


@pytest.mark.asyncio
async def test_still_picks_up_legacy_videos_key() -> None:
    client = _FakeClient(history_outputs={
        "31": {
            "videos": [
                {"filename": "clip.mp4", "subfolder": "", "type": "output"},
            ],
        },
    })
    blob = await _generator(client).generate(
        character=_character(), positive="a girl yawns",
    )
    assert blob == b"FAKE_MP4_BYTES"


@pytest.mark.asyncio
async def test_still_picks_up_gifs_key() -> None:
    client = _FakeClient(history_outputs={
        "31": {
            "gifs": [
                {"filename": "clip.webm", "subfolder": "", "type": "output"},
            ],
        },
    })
    blob = await _generator(client).generate(
        character=_character(), positive="a girl waves",
    )
    assert blob == b"FAKE_MP4_BYTES"


@pytest.mark.asyncio
async def test_ignores_image_entries_when_only_png() -> None:
    # Defensive: a workflow that only writes a PNG (no video) shouldn't
    # be silently treated as a successful video — fall through to
    # ``VideoNoOutputError`` so the caller can degrade to image post.
    client = _FakeClient(history_outputs={
        "9": {
            "images": [
                {"filename": "preview.png", "subfolder": "", "type": "output"},
            ],
        },
    })
    with pytest.raises(VideoNoOutputError):
        await _generator(client).generate(
            character=_character(), positive="a girl smiles",
        )


@pytest.mark.asyncio
async def test_empty_outputs_raise_no_output_error() -> None:
    client = _FakeClient(history_outputs={})
    with pytest.raises(VideoNoOutputError):
        await _generator(client).generate(
            character=_character(), positive="anything",
        )


@pytest.mark.asyncio
async def test_two_stage_workflow_prefers_stylized_over_raw() -> None:
    """Two-stage Wan→Illustrious workflow emits both a raw Wan clip and
    a stylized clip. The deliverable is the stylized one — picking the
    raw would surface unstyled output to the user despite the operator
    paying for the stylization pass."""
    client = _FakeClient(history_outputs={
        # raw save node (Wan-only output)
        "31": {
            "images": [
                {"filename": "raw_00001_.mp4", "subfolder": "kokoro/feed/char-x/raw", "type": "output"},
            ],
            "animated": [True],
        },
        # stylized save node (Illustrious vid2vid output)
        "115": {
            "images": [
                {"filename": "styled_00001_.mp4", "subfolder": "kokoro/feed/char-x/stylized", "type": "output"},
            ],
            "animated": [True],
        },
    })
    blob = await _generator(client).generate(
        character=_character(), positive="a girl tilts her head",
    )
    assert blob == b"FAKE_MP4_BYTES"
    # Exactly one download — the stylized file.
    assert client.downloaded == [
        ("styled_00001_.mp4", "kokoro/feed/char-x/stylized", "output"),
    ]


@pytest.mark.asyncio
async def test_falls_back_to_first_when_no_stylized_subfolder() -> None:
    """Legacy single-stage workflow only has one save node and writes
    under the bare prefix — the stylized-preference filter must not
    starve those operators by demanding a ``stylized/`` subfolder."""
    client = _FakeClient(history_outputs={
        "31": {
            "images": [
                {"filename": "clip_00001_.mp4", "subfolder": "kokoro/feed", "type": "output"},
            ],
            "animated": [True],
        },
    })
    blob = await _generator(client).generate(
        character=_character(), positive="a girl yawns",
    )
    assert blob == b"FAKE_MP4_BYTES"
    assert client.downloaded == [("clip_00001_.mp4", "kokoro/feed", "output")]
