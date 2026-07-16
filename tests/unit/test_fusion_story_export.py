"""C0-3 exports: Markdown / TXT / EPUB, pure Core, no cloud deps."""

from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree

import pytest

from kokoro_link.application.services.fusion_story_export import (
    EXPORT_FORMATS,
    export_fusion_story,
)
from kokoro_link.domain.entities.fusion_story import FusionStory


def _ready_story(
    *,
    title: str = "星夜咖啡館",
    premise: str = "兩人在深夜相遇。",
    full_text: str = "第一段。\n\n第二段有 <標籤> & 符號。",
) -> FusionStory:
    story = FusionStory.create_pending(
        character_ids=["c-a", "c-b"], prompt="提示",
    )
    from dataclasses import replace

    story = replace(story, title=title, premise=premise)
    return story.with_full_text(full_text)


class TestMarkdownAndTxt:
    def test_markdown_contains_title_premise_and_text(self) -> None:
        exported = export_fusion_story(_ready_story(), format="markdown")
        body = exported.blob.decode("utf-8")
        assert "# 星夜咖啡館" in body
        assert "> 兩人在深夜相遇。" in body
        assert "第一段。" in body and "第二段有 <標籤> & 符號。" in body
        assert exported.filename.endswith(".md")
        assert exported.media_type == "text/markdown; charset=utf-8"

    def test_txt_contains_full_content(self) -> None:
        exported = export_fusion_story(_ready_story(), format="txt")
        body = exported.blob.decode("utf-8")
        assert body.startswith("星夜咖啡館")
        assert "兩人在深夜相遇。" in body
        assert "第二段有 <標籤> & 符號。" in body
        assert exported.filename.endswith(".txt")

    def test_falls_back_to_joined_beats_without_full_text(self) -> None:
        # Defensive path: a ready story should always have full_text,
        # but the exporter must not silently emit an empty file.
        story = _ready_story(full_text="x")
        from dataclasses import replace

        story = replace(story, full_text="")
        exported = export_fusion_story(story, format="txt")
        assert "星夜咖啡館" in exported.blob.decode("utf-8")


class TestEpub:
    def test_epub_structure_is_valid(self) -> None:
        exported = export_fusion_story(_ready_story(), format="epub")
        assert exported.filename.endswith(".epub")
        assert exported.media_type == "application/epub+zip"

        archive = zipfile.ZipFile(io.BytesIO(exported.blob))
        names = archive.namelist()
        # mimetype must be the first entry and stored uncompressed.
        assert names[0] == "mimetype"
        info = archive.getinfo("mimetype")
        assert info.compress_type == zipfile.ZIP_STORED
        assert archive.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names
        assert "OEBPS/content.opf" in names
        assert "OEBPS/nav.xhtml" in names
        assert "OEBPS/chapter1.xhtml" in names

        # All XML members must parse; chapter must escape HTML-sensitive
        # characters and still carry the full content.
        for member in (
            "META-INF/container.xml",
            "OEBPS/content.opf",
            "OEBPS/nav.xhtml",
            "OEBPS/chapter1.xhtml",
        ):
            ElementTree.fromstring(archive.read(member))
        chapter = archive.read("OEBPS/chapter1.xhtml").decode("utf-8")
        assert "&lt;標籤&gt; &amp; 符號" in chapter
        assert "星夜咖啡館" in chapter

    def test_opf_carries_identifier_and_title(self) -> None:
        story = _ready_story()
        exported = export_fusion_story(story, format="epub")
        archive = zipfile.ZipFile(io.BytesIO(exported.blob))
        opf = archive.read("OEBPS/content.opf").decode("utf-8")
        assert story.id in opf
        assert "星夜咖啡館" in opf


class TestGuards:
    def test_unknown_format_rejected(self) -> None:
        with pytest.raises(ValueError):
            export_fusion_story(_ready_story(), format="pdf")

    def test_formats_constant_lists_three(self) -> None:
        assert set(EXPORT_FORMATS) == {"markdown", "txt", "epub"}

    def test_filename_sanitizes_unsafe_characters(self) -> None:
        story = _ready_story(title='a/b\\c:d*e?"<>|')
        exported = export_fusion_story(story, format="txt")
        for ch in '/\\:*?"<>|':
            assert ch not in exported.filename
