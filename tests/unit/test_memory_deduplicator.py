"""Unit tests for bigram Jaccard memory deduplication."""

import pytest

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.deduplicator import bigram_jaccard, deduplicate


def _item(content: str, kind: MemoryKind = MemoryKind.SEMANTIC) -> MemoryItem:
    return MemoryItem.create(
        character_id="char-1",
        conversation_id="conv-1",
        kind=kind,
        content=content,
        salience=0.5,
    )


class TestBigramJaccard:
    def test_identical_strings(self) -> None:
        assert bigram_jaccard("使用者住在東京", "使用者住在東京") == pytest.approx(1.0)

    def test_completely_different(self) -> None:
        assert bigram_jaccard("你好世界", "再見朋友") == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        score = bigram_jaccard("使用者喜歡音樂", "使用者喜歡閱讀")
        assert 0.2 < score < 0.8

    def test_empty_string(self) -> None:
        assert bigram_jaccard("", "hello") == pytest.approx(0.0)
        assert bigram_jaccard("", "") == pytest.approx(0.0)

    def test_single_char(self) -> None:
        assert bigram_jaccard("a", "a") == pytest.approx(1.0)
        assert bigram_jaccard("a", "b") == pytest.approx(0.0)


class TestDeduplicate:
    def test_no_existing_returns_all(self) -> None:
        new = [_item("使用者住在東京"), _item("使用者喜歡爵士")]
        result = deduplicate(new, [])
        assert len(result) == 2

    def test_exact_duplicate_filtered(self) -> None:
        existing = [_item("使用者住在東京")]
        new = [_item("使用者住在東京")]
        result = deduplicate(new, existing)
        assert len(result) == 0

    def test_near_duplicate_filtered(self) -> None:
        existing = [_item("使用者住在東京都")]
        new = [_item("使用者住在東京都附近")]
        result = deduplicate(new, existing)
        assert len(result) == 0

    def test_different_content_kept(self) -> None:
        existing = [_item("使用者住在東京")]
        new = [_item("使用者喜歡彈吉他")]
        result = deduplicate(new, existing)
        assert len(result) == 1
        assert result[0].content == "使用者喜歡彈吉他"

    def test_different_kind_not_deduped(self) -> None:
        """Same content but different kinds should not be considered duplicates."""
        existing = [_item("使用者住在東京", kind=MemoryKind.SEMANTIC)]
        new = [_item("使用者住在東京", kind=MemoryKind.EPISODIC)]
        result = deduplicate(new, existing)
        assert len(result) == 1

    def test_intra_batch_dedup(self) -> None:
        """Duplicates within the new batch itself should be filtered."""
        new = [_item("使用者住在東京"), _item("使用者住在東京")]
        result = deduplicate(new, [])
        assert len(result) == 1

    def test_custom_threshold(self) -> None:
        existing = [_item("使用者住在東京都")]
        new = [_item("使用者住在東京都附近")]
        # Very high threshold — should not filter
        result = deduplicate(new, existing, threshold=0.99)
        assert len(result) == 1

    def test_mixed_keep_and_filter(self) -> None:
        existing = [_item("使用者住在東京")]
        new = [
            _item("使用者住在東京都"),  # near duplicate — filtered
            _item("今天聊了很多關於音樂的話題"),  # unique — kept
        ]
        result = deduplicate(new, existing)
        assert len(result) == 1
        assert "音樂" in result[0].content
