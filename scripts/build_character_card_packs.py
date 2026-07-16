"""Build the repo-shipped ``.lumecard`` marketplace packs.

The bundled packs are binary zip files, so this script keeps their
manifest updates reproducible and makes it harder to hand-edit a corrupt
card. It intentionally uses the production packager.
"""

from __future__ import annotations

import json
from pathlib import Path

from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.infrastructure.character_card.packager import (
    pack_character_card,
    unpack_character_card,
)

ROOT = Path(__file__).resolve().parents[1]
PACK_DIR = ROOT / "src" / "kokoro_link" / "data" / "character_cards"

IDENTITY_PATCHES = {
    "mio_cafe_idol": {
        "gender_identity": "女性",
        "third_person_pronoun": "她",
        "visual_gender_presentation": "feminine young woman",
    },
    "chiyo_quiet_bookshop": {
        "gender_identity": "女性",
        "third_person_pronoun": "她",
        "visual_gender_presentation": "feminine adult woman",
    },
}


def main() -> int:
    PACK_DIR.mkdir(parents=True, exist_ok=True)
    for pack_id, identity_fields in IDENTITY_PATCHES.items():
        _patch_existing_pack(pack_id, identity_fields)
    _write_ren_night_market_runner_pack()
    return 0


def _patch_existing_pack(
    pack_id: str,
    identity_fields: dict[str, str],
) -> None:
    path = PACK_DIR / f"{pack_id}.lumecard"
    unpacked = unpack_character_card(path.read_bytes())
    manifest = dict(unpacked.manifest)
    character = dict(manifest["character"])
    character.update(identity_fields)
    manifest["character"] = character

    blob = pack_character_card(
        manifest_json=_to_manifest_json(manifest),
        stage_images=_ordered_stage_images(unpacked.stage_images, manifest),
        arc_templates=_ordered_arc_templates(unpacked.arc_templates, manifest),
    )
    path.write_bytes(blob)


def _write_ren_night_market_runner_pack() -> None:
    template = ArcTemplate.create(
        id="night_market_runner",
        title="河堤上的行動餐車夢",
        premise=(
            "他一邊幫家裡夜市攤收尾，一邊準備把自己的小餐車計畫做成真的。"
        ),
        theme="ambition",
        duration_days=14,
        tone="daily",
        binding=ArcTemplateBinding(world_frames=("modern",)),
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="收攤後的河堤",
                summary=(
                    "夜市燈泡一盞盞熄掉後，蓮把最後一箱餐具搬回倉庫，"
                    "換上跑鞋沿著河堤慢跑。他口袋裡塞著一張皺掉的餐車申請表，"
                    "每跑過一盞路燈，就更清楚自己其實不想永遠只替家裡顧攤。"
                ),
                tension="setup",
                scene_type="revelation",
                location="河堤慢跑道",
                dramatic_question="他會把那張申請表拿出來面對嗎？",
            ),
            ArcTemplateBeat.create(
                sequence=1,
                day_offset=3,
                title="父親的老食譜",
                summary=(
                    "蓮在整理攤車時翻到父親年輕時寫的醬汁筆記。"
                    "那些被油漬暈開的字讓他第一次意識到，家裡的夜市攤也曾經是某個人的冒險。"
                    "他想問父親當年為什麼停下，卻又怕這個問題聽起來像是在否定現在的一切。"
                ),
                tension="rising",
                scene_type="encounter",
                location="夜市攤後方",
                scene_characters=("父親",),
                dramatic_question="他能問出口，還是會繼續裝作只是幫忙？",
            ),
            ArcTemplateBeat.create(
                sequence=2,
                day_offset=7,
                title="雨夜試做",
                summary=(
                    "一場突來的雨讓夜市提前收攤，蓮和老朋友阿翔借了半邊騎樓試做新菜。"
                    "塑膠棚被雨打得很吵，鍋裡的香氣卻慢慢讓路過的人停下腳步。"
                    "當第一個陌生客人說願意付錢時，他發現自己的手心比比賽起跑前還要濕。"
                ),
                tension="rising",
                scene_type="encounter",
                location="雨夜騎樓",
                scene_characters=("阿翔",),
                dramatic_question="他敢把這次試做當成真正的開始嗎？",
            ),
            ArcTemplateBeat.create(
                sequence=3,
                day_offset=10,
                title="餐車名牌",
                summary=(
                    "蓮終於把手繪的餐車名牌帶回家。父親沒有立刻評論，只把那塊木板翻來覆去看了很久。"
                    "晚餐桌上氣氛安靜得像考場，他必須在家人的擔心、自己的期待與現實成本之間，"
                    "說清楚這不是一時衝動。"
                ),
                tension="climax",
                scene_type="conflict",
                location="家中餐桌",
                scene_characters=("父親",),
                dramatic_question="他能讓家人相信這是準備好的決定嗎？",
            ),
            ArcTemplateBeat.create(
                sequence=4,
                day_offset=13,
                title="第一盞小燈",
                summary=(
                    "試營運的夜晚，蓮在攤車前掛上第一盞小燈。"
                    "他還是會緊張，還是會在客人排隊時忘記呼吸，但當熟悉的夜市聲音從四面八方湧來，"
                    "他第一次覺得自己不是離開家裡的攤，而是把那份手藝推向下一段路。"
                ),
                tension="resolution",
                scene_type="resolution",
                location="夜市入口",
                scene_characters=("父親", "阿翔"),
                dramatic_question="他要怎麼面對第一個真正屬於自己的夜晚？",
            ),
        ],
    )

    manifest = {
        "schema_version": 1,
        "card": {
            "title": "森野 蓮 — 河堤上的行動餐車夢",
            "author": "Yuralume",
            "description": (
                "幫家裡顧夜市攤的慢熱青年，收攤後沿河堤跑步，"
                "把自己的行動餐車夢一點一點做成真的。"
            ),
            "tags": ["現代", "日常", "青年", "示範角色"],
            "note": "適合寫實或清爽日常風格；本卡未附舞台圖，安裝後可自行生成或上傳。",
            "app_version": "",
            "created_at": "",
        },
        "character": {
            "name": "森野 蓮",
            "summary": "在夜市幫家裡顧攤的青年，收攤後會沿河堤慢跑。",
            "personality": ["穩重", "慢熱", "對熟人很照顧"],
            "interests": ["夜市小吃", "長跑", "舊式掌上遊戲機"],
            "speaking_style": "warm",
            "boundaries": ["不喜歡被逼著立刻表態"],
            "aspirations": ["存夠錢開一台自己的行動餐車"],
            "appearance": "短黑髮，常穿深色防風外套和跑鞋，笑起來有點靦腆。",
            "gender_identity": "男性",
            "third_person_pronoun": "他",
            "visual_gender_presentation": "masculine young man",
            "date_of_birth": None,
            "disposition": {
                "self_centeredness": "medium",
                "candor": "medium",
                "sharing_drive": "medium",
                "associativeness": "high",
            },
            "world_frame": "modern",
            "world_awareness_enabled": False,
            "world_topics": [],
            "subscribed_categories": [],
            "excluded_topics": [],
            "proactive_enabled": True,
            "proactive_daily_limit": 3,
            "proactive_cooldown_minutes": 45,
            "accepts_web_proactive": True,
            "feed_daily_limit": 3,
            "allowed_tools": ["generate_image", "web_fetch", "web_search"],
            "companions": [
                {
                    "id": None,
                    "name": "阿翔",
                    "role": "從小一起混夜市的朋友，嘴上愛吐槽但很可靠",
                    "brief_profile": "",
                    "personality_sketch": [],
                    "relationship_snippet": "",
                }
            ],
            "arc_template_ref": "night_market_runner",
        },
        "stage_images": [],
        "bundled_arc_templates": ["night_market_runner"],
    }
    blob = pack_character_card(
        manifest_json=_to_manifest_json(manifest),
        stage_images=[],
        arc_templates=[
            ("night_market_runner.yaml", dump_arc_template_to_yaml(template)),
        ],
    )
    (PACK_DIR / "ren_night_market_runner.lumecard").write_bytes(blob)


def _ordered_stage_images(
    images: dict[str, bytes],
    manifest: dict,
) -> list[tuple[str, bytes]]:
    ordered: list[tuple[str, bytes]] = []
    used: set[str] = set()
    for member in manifest.get("stage_images", []):
        if member in images:
            ordered.append((member, images[member]))
            used.add(member)
    for member, data in sorted(images.items()):
        if member not in used:
            ordered.append((member, data))
    return ordered


def _ordered_arc_templates(
    templates: dict[str, str],
    manifest: dict,
) -> list[tuple[str, str]]:
    ordered: list[tuple[str, str]] = []
    used: set[str] = set()
    for template_id in manifest.get("bundled_arc_templates", []):
        filename = f"{template_id}.yaml"
        if filename in templates:
            ordered.append((filename, templates[filename]))
            used.add(filename)
    for filename, text in sorted(templates.items()):
        if filename not in used:
            ordered.append((filename, text))
    return ordered


def _to_manifest_json(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
