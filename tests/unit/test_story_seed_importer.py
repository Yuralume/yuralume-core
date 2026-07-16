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
