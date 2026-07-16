"""add characters.voice_profile_json

Per-character TTS override (ref audio, prompt, dubbing target lang,
GPT/SoVITS weight paths). NULL = use the global ``TTSSettings``.

JSON-encoded text column rather than flat fields so future ``VoiceProfile``
additions (e.g. per-character speed_factor, custom backend pick) don't
need another migration. Mirror of the ``feature_models_json`` /
``image_trigger_patterns`` pattern already in the schema.

Revision ID: bj9n1i70034
Revises: bi8m0h60033
Create Date: 2026-05-01 16:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bj9n1i70034"
down_revision: Union[str, None] = "bi8m0h60033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("voice_profile_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("characters", "voice_profile_json")
