"""YAML seed pack importer — idempotent upsert on external_id."""

from pathlib import Path

import pytest

from kokoro_link.application.services.story_seed_importer import (
    StorySeedImporter,
    default_pack_paths,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStorySeedRepository,
)


@pytest.mark.asyncio
async def test_import_bundled_packs_populates_repo(tmp_path: Path) -> None:
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths(default_pack_paths())

    # At least the 4 bundled packs land.
    assert report.packs >= 4
    assert report.seeds_seen > 50
    # All loaded — repo now holds at least that many seeds.
    all_seeds = await repo.list_for_character(
        "any-character", include_global=True, enabled_only=False,
    )
    assert len(all_seeds) >= report.seeds_seen


@pytest.mark.asyncio
async def test_import_is_idempotent(tmp_path: Path) -> None:
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)
    paths = default_pack_paths()

    first = await importer.import_paths(paths)
    count_after_first = len(
        await repo.list_for_character("any", enabled_only=False),
    )
    # Run again — same pack, same external_ids → upsert, no duplicates.
    await importer.import_paths(paths)
    count_after_second = len(
        await repo.list_for_character("any", enabled_only=False),
    )
    assert count_after_first == count_after_second
    assert first.seeds_seen == count_after_first


@pytest.mark.asyncio
async def test_import_custom_yaml(tmp_path: Path) -> None:
    pack_file = tmp_path / "custom.yaml"
    pack_file.write_text(
        "pack_id: custom_pack\n"
        "seeds:\n"
        "  - external_id: custom:a:001\n"
        "    seed_text: 一個自訂種子\n"
        "    world_frames: [modern]\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])
    assert report.seeds_seen == 1
    assert report.inserted == 1

    stored = await repo.list_by_pack("custom_pack")
    assert len(stored) == 1
    assert stored[0].external_id == "custom:a:001"
    # Bundled / custom packs default to zh-TW provenance.
    assert stored[0].language == "zh-TW"


class _StubSeedTranslator:
    def __init__(self, *, fail: bool = False, wrong_length: bool = False) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self._fail = fail
        self._wrong_length = wrong_length

    async def translate_seed_texts(self, seed_texts, *, target_language):
        self.calls.append((tuple(seed_texts), target_language))
        if self._fail:
            raise RuntimeError("translator down")
        if self._wrong_length:
            return list(seed_texts)[:-1]  # drop one → mismatch
        return [f"EN::{t}" for t in seed_texts]


def _custom_pack(tmp_path: Path) -> Path:
    pack_file = tmp_path / "custom.yaml"
    pack_file.write_text(
        "pack_id: custom_pack\n"
        "language: zh-TW\n"
        "seeds:\n"
        "  - external_id: custom:a:001\n"
        "    seed_text: 一個自訂種子\n"
        "  - external_id: custom:a:002\n"
        "    seed_text: 另一個自訂種子\n",
        encoding="utf-8",
    )
    return pack_file


@pytest.mark.asyncio
async def test_translate_localizes_seed_text_and_stamps_language(
    tmp_path: Path,
) -> None:
    translator = _StubSeedTranslator()
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo, translator=translator)

    await importer.import_paths([_custom_pack(tmp_path)], target_language="en-US")

    assert translator.calls[0][1] == "en-US"
    stored = sorted(
        await repo.list_by_pack("custom_pack"), key=lambda s: s.external_id,
    )
    assert stored[0].seed_text == "EN::一個自訂種子"
    assert stored[0].language == "en-US"
    assert stored[1].seed_text == "EN::另一個自訂種子"


@pytest.mark.asyncio
async def test_translate_without_target_language_is_noop(tmp_path: Path) -> None:
    translator = _StubSeedTranslator()
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo, translator=translator)

    await importer.import_paths([_custom_pack(tmp_path)])

    assert translator.calls == []
    stored = await repo.list_by_pack("custom_pack")
    assert stored[0].seed_text.startswith("一個") or stored[0].seed_text.startswith("另一")


@pytest.mark.asyncio
async def test_translate_failure_lands_original_text(tmp_path: Path) -> None:
    translator = _StubSeedTranslator(fail=True)
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo, translator=translator)

    report = await importer.import_paths(
        [_custom_pack(tmp_path)], target_language="ja-JP",
    )

    assert report.inserted == 2
    stored = sorted(
        await repo.list_by_pack("custom_pack"), key=lambda s: s.external_id,
    )
    # Original zh-TW text preserved; language unchanged (no false badge).
    assert stored[0].seed_text == "一個自訂種子"
    assert stored[0].language == "zh-TW"


# ---- tier / regions parsing -----------------------------------------


@pytest.mark.asyncio
async def test_import_parses_tier_and_regions(tmp_path: Path) -> None:
    pack_file = tmp_path / "regional.yaml"
    pack_file.write_text(
        "pack_id: regional_pack\n"
        "seeds:\n"
        "  - external_id: reg:a:001\n"
        "    seed_text: 放學路上買了手搖飲\n"
        "    tier: daily\n"
        "    regions: [tw]\n"
        "  - external_id: reg:a:002\n"
        "    seed_text: 部活結束後在自販機前碰到熟人\n"
        "    tier: dramatic\n"
        "    regions: [jp, global]\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])
    assert report.errors == ()

    stored = sorted(
        await repo.list_by_pack("regional_pack"), key=lambda s: s.external_id,
    )
    assert stored[0].tier == "daily"
    assert stored[0].regions == ("tw",)
    assert stored[1].tier == "dramatic"
    assert stored[1].regions == ("jp", "global")


@pytest.mark.asyncio
async def test_import_defaults_tier_daily_and_regions_global(
    tmp_path: Path,
) -> None:
    pack_file = tmp_path / "legacy.yaml"
    pack_file.write_text(
        "pack_id: legacy_pack\n"
        "seeds:\n"
        "  - external_id: leg:a:001\n"
        "    seed_text: 沒帶新欄位的舊種子\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])
    assert report.errors == ()

    stored = await repo.list_by_pack("legacy_pack")
    assert stored[0].tier == "daily"
    assert stored[0].regions == ("global",)


@pytest.mark.asyncio
async def test_import_invalid_tier_errors_without_aborting_pack(
    tmp_path: Path,
) -> None:
    pack_file = tmp_path / "badtier.yaml"
    pack_file.write_text(
        "pack_id: badtier_pack\n"
        "seeds:\n"
        "  - external_id: bad:a:001\n"
        "    seed_text: 非法層級\n"
        "    tier: weekly\n"
        "  - external_id: bad:a:002\n"
        "    seed_text: 正常種子\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])

    assert report.skipped == 1
    assert any("bad:a:001" in e for e in report.errors)
    stored = await repo.list_by_pack("badtier_pack")
    assert [s.external_id for s in stored] == ["bad:a:002"]


@pytest.mark.asyncio
async def test_import_unknown_region_errors_without_aborting_pack(
    tmp_path: Path,
) -> None:
    pack_file = tmp_path / "badregion.yaml"
    pack_file.write_text(
        "pack_id: badregion_pack\n"
        "seeds:\n"
        "  - external_id: badr:a:001\n"
        "    seed_text: 非法地區\n"
        "    regions: [taiwan]\n"
        "  - external_id: badr:a:002\n"
        "    seed_text: 正常地區\n"
        "    regions: [west]\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])

    assert report.skipped == 1
    assert any("badr:a:001" in e for e in report.errors)
    stored = await repo.list_by_pack("badregion_pack")
    assert [s.external_id for s in stored] == ["badr:a:002"]
    assert stored[0].regions == ("west",)


@pytest.mark.asyncio
async def test_import_regions_must_be_a_list(tmp_path: Path) -> None:
    pack_file = tmp_path / "badshape.yaml"
    pack_file.write_text(
        "pack_id: badshape_pack\n"
        "seeds:\n"
        "  - external_id: shape:a:001\n"
        "    seed_text: regions 給了字串\n"
        "    regions: tw\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths([pack_file])

    assert report.skipped == 1
    assert any("shape:a:001" in e for e in report.errors)


@pytest.mark.asyncio
async def test_reimport_updates_tier_and_regions(tmp_path: Path) -> None:
    """Idempotent upsert must carry the new structural fields, so bumping
    an existing pack's tier/regions in YAML lands on re-import."""
    pack_file = tmp_path / "evolve.yaml"
    pack_file.write_text(
        "pack_id: evolve_pack\n"
        "seeds:\n"
        "  - external_id: ev:a:001\n"
        "    seed_text: 起初是全球日常\n",
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)
    await importer.import_paths([pack_file])

    pack_file.write_text(
        "pack_id: evolve_pack\n"
        "seeds:\n"
        "  - external_id: ev:a:001\n"
        "    seed_text: 起初是全球日常\n"
        "    tier: dramatic\n"
        "    regions: [jp]\n",
        encoding="utf-8",
    )
    await importer.import_paths([pack_file])

    stored = await repo.list_by_pack("evolve_pack")
    assert len(stored) == 1
    assert stored[0].tier == "dramatic"
    assert stored[0].regions == ("jp",)


@pytest.mark.asyncio
async def test_export_yaml_round_trips_tier_and_regions(tmp_path: Path) -> None:
    """The export CLI's YAML payload must carry tier/regions, or an
    export → re-import cycle silently resets a dramatic/regional seed
    back to daily/global."""
    import yaml

    from kokoro_link.cli.export_story_seeds import _seed_to_yaml
    from kokoro_link.domain.entities.story_seed import StorySeed

    seed = StorySeed.create(
        seed_text="祭典夜裡的告白",
        tier="dramatic",
        regions=["jp"],
        external_id="rt:a:001",
        pack_id="rt_pack",
    )
    payload = _seed_to_yaml(seed, with_local=False)
    assert payload["tier"] == "dramatic"
    assert payload["regions"] == ["jp"]

    # Defaults stay omitted (keeps shipped YAML terse).
    default_seed = StorySeed.create(
        seed_text="日常", external_id="rt:a:002", pack_id="rt_pack",
    )
    default_payload = _seed_to_yaml(default_seed, with_local=False)
    assert "tier" not in default_payload
    assert "regions" not in default_payload

    # Full loop: exported YAML re-imports to the same structural fields.
    pack_file = tmp_path / "roundtrip.yaml"
    pack_file.write_text(
        yaml.safe_dump(
            {"pack_id": "rt_pack", "seeds": [payload]}, allow_unicode=True,
        ),
        encoding="utf-8",
    )
    repo = InMemoryStorySeedRepository()
    report = await StorySeedImporter(repo).import_paths([pack_file])
    assert report.errors == ()
    stored = await repo.list_by_pack("rt_pack")
    assert stored[0].tier == "dramatic"
    assert stored[0].regions == ("jp",)


@pytest.mark.asyncio
async def test_translate_length_mismatch_lands_original(tmp_path: Path) -> None:
    translator = _StubSeedTranslator(wrong_length=True)
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo, translator=translator)

    await importer.import_paths(
        [_custom_pack(tmp_path)], target_language="en-US",
    )
    stored = sorted(
        await repo.list_by_pack("custom_pack"), key=lambda s: s.external_id,
    )
    assert stored[0].seed_text == "一個自訂種子"
    assert stored[0].language == "zh-TW"
