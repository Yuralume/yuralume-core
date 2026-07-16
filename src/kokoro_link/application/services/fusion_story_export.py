"""Fusion-story exports — Markdown / TXT / EPUB (Creator Studio C0).

Pure functions over a terminal :class:`FusionStory`: no LLM, no cloud
dependency, stdlib-only EPUB packaging so self-host stays dependency
free. The route layer owns status/ownership checks; this module only
renders content.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

from kokoro_link.domain.entities.fusion_story import FusionStory


EXPORT_FORMAT_MARKDOWN = "markdown"
EXPORT_FORMAT_TXT = "txt"
EXPORT_FORMAT_EPUB = "epub"

EXPORT_FORMATS = (
    EXPORT_FORMAT_MARKDOWN,
    EXPORT_FORMAT_TXT,
    EXPORT_FORMAT_EPUB,
)

_FILENAME_UNSAFE = set('/\\:*?"<>|')
_MAX_FILENAME_STEM = 60


@dataclass(frozen=True, slots=True)
class ExportedFusionStory:
    filename: str
    blob: bytes
    media_type: str


def export_fusion_story(
    story: FusionStory, *, format: str,
) -> ExportedFusionStory:
    if format == EXPORT_FORMAT_MARKDOWN:
        return ExportedFusionStory(
            filename=f"{_filename_stem(story)}.md",
            blob=_render_markdown(story).encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
        )
    if format == EXPORT_FORMAT_TXT:
        return ExportedFusionStory(
            filename=f"{_filename_stem(story)}.txt",
            blob=_render_txt(story).encode("utf-8"),
            media_type="text/plain; charset=utf-8",
        )
    if format == EXPORT_FORMAT_EPUB:
        return ExportedFusionStory(
            filename=f"{_filename_stem(story)}.epub",
            blob=_build_epub(story),
            media_type="application/epub+zip",
        )
    raise ValueError(f"unknown export format: {format!r}")


def _story_text(story: FusionStory) -> str:
    """Prefer the polished full text; fall back to joined beats so the
    exporter can never emit an empty body for a story with prose."""
    text = (story.full_text or "").strip()
    if text:
        return text
    return story.joined_text().strip()


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _filename_stem(story: FusionStory) -> str:
    stem = "".join(
        ch for ch in (story.title or "").strip()
        if ch not in _FILENAME_UNSAFE and ch not in "\r\n\t"
    ).strip()
    if not stem:
        stem = f"fusion-story-{story.id[:8]}"
    return stem[:_MAX_FILENAME_STEM]


def _render_markdown(story: FusionStory) -> str:
    parts = [f"# {story.title}".rstrip()]
    premise = (story.premise or "").strip()
    if premise:
        parts.append(f"> {premise}")
    parts.append(_story_text(story))
    return "\n\n".join(parts) + "\n"


def _render_txt(story: FusionStory) -> str:
    parts = [(story.title or "").strip()]
    premise = (story.premise or "").strip()
    if premise:
        parts.append(premise)
    parts.append(_story_text(story))
    return "\n\n".join(p for p in parts if p) + "\n"


# ---- EPUB 3 (stdlib zipfile only) ------------------------------------


_CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def _build_epub(story: FusionStory) -> bytes:
    title = escape((story.title or "").strip() or "Fusion Story")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Per OCF spec the mimetype entry comes first and is stored
        # uncompressed so readers can sniff it from the raw bytes.
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", _CONTAINER_XML)
        archive.writestr("OEBPS/content.opf", _render_opf(story, title))
        archive.writestr("OEBPS/nav.xhtml", _render_nav(title))
        archive.writestr(
            "OEBPS/chapter1.xhtml", _render_chapter(story, title),
        )
    return buffer.getvalue()


def _render_opf(story: FusionStory, title: str) -> str:
    modified = story.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">urn:yuralume:fusion-story:{escape(story.id)}</dc:identifier>
    <dc:title>{title}</dc:title>
    <dc:language>und</dc:language>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="chapter1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine>
    <itemref idref="chapter1"/>
  </spine>
</package>
"""


def _render_nav(title: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>{title}</title></head>
  <body>
    <nav epub:type="toc">
      <ol><li><a href="chapter1.xhtml">{title}</a></li></ol>
    </nav>
  </body>
</html>
"""


def _render_chapter(story: FusionStory, title: str) -> str:
    premise = escape((story.premise or "").strip())
    body_parts = [f"<h1>{title}</h1>"]
    if premise:
        body_parts.append(f"<blockquote><p>{premise}</p></blockquote>")
    for paragraph in _paragraphs(_story_text(story)):
        lines = "<br/>".join(
            escape(line) for line in paragraph.splitlines()
        )
        body_parts.append(f"<p>{lines}</p>")
    body = "\n    ".join(body_parts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>{title}</title></head>
  <body>
    {body}
  </body>
</html>
"""
