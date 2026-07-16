"""Persistence contract checks for multi-character short stories."""

from sqlalchemy import Text

from kokoro_link.infrastructure.persistence.fusion_story_models import (
    FusionStoryRow,
    FusionStoryVersionRow,
)


def test_fusion_story_themes_accept_narrative_length_text() -> None:
    """LLM-produced themes are player-visible prose, not 64-char tags."""
    assert isinstance(FusionStoryRow.__table__.c.theme.type, Text)
    assert isinstance(FusionStoryVersionRow.__table__.c.theme.type, Text)
