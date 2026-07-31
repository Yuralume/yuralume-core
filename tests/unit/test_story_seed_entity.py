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


# ---- tier ------------------------------------------------------------


def test_create_defaults_to_daily_tier() -> None:
    seed = StorySeed.create(seed_text="s")
    assert seed.tier == "daily"


def test_create_accepts_dramatic_tier() -> None:
    seed = StorySeed.create(seed_text="s", tier="dramatic")
    assert seed.tier == "dramatic"


def test_create_rejects_unknown_tier() -> None:
    with pytest.raises(ValueError):
        StorySeed.create(seed_text="s", tier="weekly")


def test_create_rejects_blank_tier() -> None:
    with pytest.raises(ValueError):
        StorySeed.create(seed_text="s", tier="   ")


def test_with_updates_changes_tier() -> None:
    seed = StorySeed.create(seed_text="s")
    updated = seed.with_updates(tier="dramatic")
    assert updated.tier == "dramatic"


def test_with_updates_rejects_unknown_tier() -> None:
    seed = StorySeed.create(seed_text="s")
    with pytest.raises(ValueError):
        seed.with_updates(tier="weekly")


def test_with_updates_none_keeps_tier() -> None:
    seed = StorySeed.create(seed_text="s", tier="dramatic")
    assert seed.with_updates(seed_text="t").tier == "dramatic"


# ---- regions ---------------------------------------------------------


def test_create_defaults_to_global_region() -> None:
    seed = StorySeed.create(seed_text="s")
    assert seed.regions == ("global",)


def test_create_empty_regions_falls_back_to_global() -> None:
    seed = StorySeed.create(seed_text="s", regions=[])
    assert seed.regions == ("global",)


def test_create_normalises_region_whitespace() -> None:
    seed = StorySeed.create(seed_text="s", regions=[" tw ", ""])
    assert seed.regions == ("tw",)


def test_fits_region_global_seed_matches_everything() -> None:
    seed = StorySeed.create(seed_text="s")  # regions=("global",)
    assert seed.fits_region("tw")
    assert seed.fits_region("jp")
    assert seed.fits_region(None)


def test_fits_region_global_wildcard_wins_even_when_mixed() -> None:
    seed = StorySeed.create(seed_text="s", regions=["global", "tw"])
    assert seed.fits_region("jp")
    assert seed.fits_region(None)


def test_fits_region_regional_seed_requires_match() -> None:
    seed = StorySeed.create(seed_text="s", regions=["tw"])
    assert seed.fits_region("tw")
    assert not seed.fits_region("jp")


def test_fits_region_none_region_only_passes_global() -> None:
    seed = StorySeed.create(seed_text="s", regions=["tw"])
    assert not seed.fits_region(None)


def test_fits_region_multi_region_seed() -> None:
    seed = StorySeed.create(seed_text="s", regions=["tw", "jp"])
    assert seed.fits_region("tw")
    assert seed.fits_region("jp")
    assert not seed.fits_region("west")


def test_with_updates_changes_regions() -> None:
    seed = StorySeed.create(seed_text="s")
    updated = seed.with_updates(regions=["jp"])
    assert updated.regions == ("jp",)


def test_with_updates_none_keeps_regions() -> None:
    seed = StorySeed.create(seed_text="s", regions=["tw"])
    assert seed.with_updates(seed_text="t").regions == ("tw",)
