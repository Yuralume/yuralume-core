"""character.companions_json + schedule_activities.companion_names_json

兩條欄位一起上：

* ``characters.companions_json`` Text NOT NULL DEFAULT ``'[]'`` —
  per-character 私人 NPC 同伴清單（同事/室友/家人…）。詳見
  :class:`kokoro_link.domain.value_objects.companion.CharacterCompanion`。

* ``schedule_activities.companion_names_json`` Text NOT NULL
  DEFAULT ``'[]'`` —— 該活動「跟誰一起」的同伴名字（或顯示名）
  陣列。schedule planner 會根據 companions 自行決定要不要填，
  prompt builder / post-turn extractor 會把它渲染成自然語句並寫進
  ``MemoryItem.participants`` (``actor_kind="npc"``)，讓記憶不再是
  獨角戲。

兩個欄位都是 server_default ``'[]'`` 而不是 nullable，跟 character 上
其他 list-shaped 欄位（``feature_models_json`` 等）一致；空陣列即「沒
有設定」的合法 baseline，舊資料不需要 backfill。

Revision ID: bw2a4b90047
Revises: bv1z3a80046
Create Date: 2026-05-13 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bw2a4b90047"
down_revision: Union[str, None] = "bv1z3a80046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "companions_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "schedule_activities",
        sa.Column(
            "companion_names_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("schedule_activities", "companion_names_json")
    op.drop_column("characters", "companions_json")
