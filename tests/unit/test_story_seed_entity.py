"""StorySeed entity validation."""

import pytest

from kokoro_link.domain.entities.story_seed import StorySeed


def test_create_trims_seed_text_and_assigns_id() -> None:
    seed = StorySeed.create(seed_text="  做了個奇怪的夢  ")
    assert seed.id
    assert seed.seed_text == "做了個奇怪的夢"


def test_create_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        StorySeed.create(seed_text="   ")


def test_create_defaults_to_any_frame() -> None:
    seed = StorySeed.create(seed_text="s")
    assert seed.world_frames == ("any",)


def test_weight_clamped_to_nonneg() -> None:
    seed = StorySeed.create(seed_text="s", weight=-1.0)
    assert seed.weight == 0.0


def test_fits_frame_any() -> None:
    seed = StorySeed.create(seed_text="s", world_frames=["any"])
    assert seed.fits_frame("modern")
    assert seed.fits_frame("fantasy")


def test_fits_frame_specific() -> None:
    seed = StorySeed.create(seed_text="s", world_frames=["modern"])
    assert seed.fits_frame("modern")
    assert not seed.fits_frame("fantasy")


def test_with_updates_bumps_updated_at() -> None:
    seed = StorySeed.create(seed_text="first")
    later = seed.with_updates(seed_text="second")
    assert later.seed_text == "second"
    assert later.updated_at >= seed.updated_at
    assert later.id == seed.id
