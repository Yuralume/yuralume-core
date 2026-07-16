"""OpenAI-compatible LLM-backed character draft generator.

Supports an optional image attachment via the standard multimodal
message shape:

    {"role": "user", "content": [
        {"type": "text", "text": "..."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
    ]}

If the selected model doesn't accept images the request will usually
fail with 400/422 or the model will simply ignore the image part and
produce bad output. On *any* exception from the image path we retry
with text-only so the user still gets a usable draft.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import date
from typing import Any

import httpx

from kokoro_link.application.services.feature_keys import FEATURE_IMAGE_RECOGNITION
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.character_draft import (
    CharacterDraft,
    CharacterDraftGeneratorPort,
    CompanionDraft,
    CharacterNameCandidate,
    ImageInput,
)
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.visual_subject import (
    normalise_visual_subject_type,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)

_LOGGER = logging.getLogger(__name__)

_MAX_NAME_CHARS = 40
_MAX_NAME_CANDIDATES = 5
_MAX_NAME_RATIONALE_CHARS = 120
_MAX_SUMMARY_CHARS = 400
_MAX_STYLE_CHARS = 120
_MAX_APPEARANCE_CHARS = 400
_MAX_IDENTITY_CHARS = 160
_MAX_LIST_ITEMS = 6
_MAX_LIST_ITEM_CHARS = 60
_MAX_COMPANIONS_IN_DRAFT = 3
_MAX_COMPANION_NAME_CHARS = 40
_MAX_COMPANION_ROLE_CHARS = 40
_MAX_COMPANION_PROFILE_CHARS = 240
_MAX_COMPANION_REL_CHARS = 160
_MAX_IMAGE_RECOGNITION_CONTEXT_CHARS = 3000
_WORLD_FRAME_VALUES = {"modern", "fantasy", "school", "custom"}


class LLMCharacterDraftGenerator(CharacterDraftGeneratorPort):
    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model_port: ChatModelPort | None = None,
        base_url: str = "",
        api_key: str | None = None,
        model: str = "",
        feature_key: str | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._resolver: ModelResolver | None = None
        self._image_recognition_resolver: ModelResolver | None = None
        if provider is not None or model_port is not None:
            self._resolver = ModelResolver(
                provider=provider, model=model_port, feature_key=feature_key,
            )
            if provider is not None:
                self._image_recognition_resolver = ModelResolver(
                    provider=provider,
                    feature_key=FEATURE_IMAGE_RECOGNITION,
                )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def generate(
        self,
        *,
        prompt: str | None,
        image: ImageInput | None,
        operator_primary_language: str = "zh-TW",
        operator_id: str | None = None,
    ) -> CharacterDraft:
        if (
            self._resolver is not None
            and await self._resolver.is_fake(operator_id=operator_id)
        ):
            return CharacterDraft()
        instruction = _build_instruction(
            prompt,
            operator_primary_language=operator_primary_language,
        )

        if image is not None:
            primary_supports_vision = await self._primary_supports_vision(
                operator_id=operator_id,
            )
            if primary_supports_vision is not False:
                try:
                    raw = await self._call(
                        instruction,
                        image=image,
                        operator_id=operator_id,
                    )
                    draft = _parse_draft(raw)
                    if draft is not None:
                        return draft
                    _LOGGER.info(
                        "Draft generator: image path returned unparseable JSON, retrying text-only",
                    )
                except Exception:
                    _LOGGER.info(
                        "Draft generator: image path failed (model likely lacks vision), retrying text-only",
                        exc_info=True,
                    )

            image_context = await self._build_image_recognition_context(
                image,
                operator_id=operator_id,
            )
            instruction = _build_instruction(
                prompt,
                image_context=image_context,
                image_unavailable=not bool(image_context),
                operator_primary_language=operator_primary_language,
            )

        raw = await self._call(instruction, image=None, operator_id=operator_id)
        draft = _parse_draft(raw)
        return draft or CharacterDraft()

    async def _primary_supports_vision(
        self,
        *,
        operator_id: str | None,
    ) -> bool | None:
        if self._resolver is None:
            return None
        try:
            model, _ = await self._resolver.resolve(operator_id=operator_id)
        except Exception:
            _LOGGER.exception(
                "Draft generator: failed to inspect primary model vision support",
            )
            return None
        return bool(getattr(model, "supports_vision", False))

    async def _build_image_recognition_context(
        self,
        image: ImageInput,
        *,
        operator_id: str | None,
    ) -> str:
        resolver = self._image_recognition_resolver
        if resolver is None:
            return ""
        try:
            model, model_id = await resolver.resolve(operator_id=operator_id)
        except Exception:
            _LOGGER.exception(
                "Draft generator: failed to resolve image recognition model",
            )
            return ""
        if not bool(getattr(model, "supports_vision", False)):
            _LOGGER.info(
                "Draft generator: image recognition route resolved to non-vision model",
            )
            return ""
        kwargs: dict[str, Any] = {"image_urls": (_image_data_url(image),)}
        if model_id is not None:
            kwargs["model"] = model_id
        try:
            raw = await model.generate(
                _build_image_recognition_instruction(),
                **kwargs,
            )
        except Exception:
            _LOGGER.exception(
                "Draft generator: image recognition preflight failed",
            )
            return ""
        return _clean_image_recognition_context(raw)

    async def _call(
        self,
        instruction: str,
        *,
        image: ImageInput | None,
        operator_id: str | None = None,
    ) -> str:
        if self._resolver is not None:
            image_urls = [_image_data_url(image)] if image is not None else []
            return await self._resolver.generate(
                instruction,
                image_urls=image_urls,
                operator_id=operator_id,
            )
        if not self._base_url or not self._model:
            raise RuntimeError("LLMCharacterDraftGenerator is not configured")
        messages = _build_messages(instruction, image)
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
        return data["choices"][0]["message"]["content"]


def _build_image_recognition_instruction() -> str:
    return (
        "你是角色創建流程的圖片識別前處理器。\n"
        "請把使用者上傳的圖片轉成可供後續純文字角色草稿模型使用的詳細文字脈絡。\n"
        "重點包含：角色外觀、髮型髮色、瞳色、服裝、配件、姿勢、表情、畫風、"
        "世界觀線索、可見文字/OCR、非人物主體類型，以及任何會影響角色設定的視覺細節。\n"
        "規則：\n"
        "- 只描述圖片能支持的內容，不要猜測真人身分、年齡、種族、國籍或敏感屬性。\n"
        "- 可以指出不確定處，例如「可能是」「看起來像」。\n"
        "- 不要輸出 JSON；用繁體中文條列即可。\n"
    )


def _clean_image_recognition_context(text: str | None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) > _MAX_IMAGE_RECOGNITION_CONTEXT_CHARS:
        return cleaned[:_MAX_IMAGE_RECOGNITION_CONTEXT_CHARS].rstrip()
    return cleaned


def _image_context_block(
    *,
    image_context: str,
    image_unavailable: bool,
) -> list[str]:
    cleaned = _clean_image_recognition_context(image_context)
    if cleaned:
        return [
            "",
            "圖片識別摘要：",
            "以下內容由多模態模型根據使用者上傳圖片產生，供目前的角色草稿模型理解圖片。",
            cleaned,
            "請把圖片摘要視為使用者提示的一部分，但不要聲稱自己直接看過圖片。",
        ]
    if image_unavailable:
        return [
            "",
            "圖片狀態：",
            "使用者有上傳圖片，但目前沒有可用的圖片識別模型；不要假裝看過圖片，請只根據文字提示保守產生草稿。",
        ]
    return []


def _image_data_url(image: ImageInput) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime_type};base64,{encoded}"


def _build_instruction(
    prompt: str | None,
    *,
    image_context: str = "",
    image_unavailable: bool = False,
    operator_primary_language: str = "zh-TW",
) -> str:
    hint = (prompt or "").strip() or "（使用者沒有提供文字提示，請從圖片推想）"
    language_hint = render_operator_language_hint(operator_primary_language)
    language_lines = [language_hint, ""] if language_hint else []
    return "\n".join(
        [
            *language_lines,
            "你是一個角色設計助手。請根據使用者的提示（可能含圖片），"
            "為一個虛擬角色產出一份草稿設定。",
            "",
            "使用者提示：",
            hint,
            *_image_context_block(
                image_context=image_context,
                image_unavailable=image_unavailable,
            ),
            "",
            "輸出規則：",
            "- 只輸出一個 JSON 物件，不要任何前言、code fence 或說明。",
            "- 物件欄位固定為以下十七個：",
            "  name (str), name_candidates (list[object]), summary (str), personality (list[str]),",
            "  interests (list[str]), speaking_style (str),",
            "  boundaries (list[str]), aspirations (list[str]), appearance (str),",
            "  gender_identity (str), third_person_pronoun (str),",
            "  visual_gender_presentation (str), visual_subject_type (str),",
            "  date_of_birth (str|null, YYYY-MM-DD), world_frame (str),",
            "  personality_type (object), companions (list[object])",
            "- 玩家會看到 JSON 中的文字欄位；name、summary、personality、"
            "interests、speaking_style、boundaries、aspirations、appearance、"
            "gender_identity、third_person_pronoun、visual_gender_presentation、"
            "companions 內所有文字都必須使用上方「玩家可見自然語言輸出語言"
            "（BCP 47 標籤）」指定的語言。",
            "- name 限 2-12 字。名字必須呼應角色氣質、世界框架與文化語境；"
            "請考慮字義、聲韻、年代感與角色生活圈，不要像隨手填表的泛用名。",
            "- name_candidates 是 3~5 個候選名，每個物件包含 name 與 rationale。"
            "先發散候選，再選最適合的一個填入 name；rationale 用一句話說明名字如何呼應角色氣質。",
            "- summary 為二到四句話的角色簡介，著重抽象氣質、背景、"
            "  人物定位、整體印象這類難以入畫的描述；僅描述角色本人，"
            "  不要在結尾額外補上對玩家或操作者的固定關係句。"
            "  若提示明確要求某種既有關係，可以自然寫入設定；否則不要替玩家預設親疏。",
            "- 對玩家的熟悉度、稱呼偏好與互動深度會由後續使用者畫像、"
            "  關係里程碑與長期記憶逐步建立，不是角色草稿欄位。",
            "- personality / interests / boundaries / aspirations 各為 1-5 個短詞或短語。",
            "- speaking_style 為 voice profile 式的一句話描述：包含語氣詞、標點習慣、"
            "訊息長短傾向與口語節奏，不只寫「溫柔」「活潑」這種抽象詞。",
            "- gender_identity 是角色自我身份 / 基本資料文字。若提示或圖片有足夠線索，"
            "  由你依整體語意合理建議；若沒有線索或角色設定不適用性別，輸出空字串。"
            "  不要用關鍵字列表或刻板印象硬猜。可輸出例如「男性」「女性」「非二元」"
            "  「無性別 AI」「中性少年」等自由文字。",
            "- third_person_pronoun 是稱呼角色本身的第三人稱代稱。若角色設定明確或你能"
            "  合理判斷，輸出使用者自然會用的代稱；若無把握輸出空字串，後續 UI 會使用角色名。"
            "  代稱也必須是上方主要語言裡自然會使用的代稱，不要因為範例含有中文就輸出中文代稱；"
            "  例如 en-US 可輸出 he / she / they / it，zh-TW 可輸出「他」「她」「TA」「它」，"
            "  其他語言請用該語言的自然代稱，不要逐字翻譯範例。",
            "- visual_gender_presentation 是給文生圖 / 視覺媒體使用的外觀性別呈現，"
            "  必須與 appearance 的視覺描述相容，但不必等同於代稱。若有圖片請以圖片為準；"
            "  若無線索輸出空字串。可輸出例如 masculine young man / androgynous teen / "
            "  feminine woman / gender-neutral android 等自由文字。",
            "- visual_subject_type 是給圖片與影片生成使用的畫面主體類型，必須是 "
            "auto / human / animal / anthropomorphic / creature / object 之一。"
            "一般人類或人形角色用 human；真實寵物、貓、狗、鳥等非人類動物用 animal；"
            "明確是獸人、furry、擬人動物才用 anthropomorphic；怪物/龍/妖精等非普通動物用 creature；"
            "物件或非生物吉祥物用 object；不確定才用 auto。",
            "- date_of_birth 是角色生日。若使用者明確提供生日、出生年或年齡，"
            "  優先依照使用者要求；否則可以依角色氣質、背景與外觀合理想像一個"
            "  參考用生日，盡量不要留空。輸出必須是 YYYY-MM-DD；只有角色設定"
            "  本身完全不適用生日時才輸出 null。",
            "- world_frame 是世界框架，必須為 modern / fantasy / school / custom 之一。"
            "  使用者有要求就照要求；否則請依角色設定合理選一個最貼近的值。",
            "- personality_type 是 16 型性格創作參考物件，欄位固定為："
            "system (固定 mbti_16), code (16 型代碼或空字串), source (llm_inferred),"
            " confidence (0.0-1.0), rationale (短句), consistency_notes (list[str])。"
            "若提示不足或信心低，code 輸出空字串且 source 輸出 unset；不要硬填。"
            "這只是角色創作參考，不是心理診斷，也不是絕對規則。",
            "- appearance 會直接餵給文生圖模型，必須是具體、可視覺化的"
            "  外觀描述，從頭到腳依序覆蓋：髮色、髮型／髮長、瞳色、"
            "  臉型與五官特徵、體型與身高氣場、膚色、上身服裝、下身服裝、"
            "  鞋子、配件／飾品（耳環、項鍊、眼鏡、帽子等）、若有則加入"
            "  代表性持物。用具體名詞與顏色，避免『氣質清冷』『眼神溫柔』"
            "  這類抽象形容（那些放在 summary）。用逗號分隔的短語堆疊，"
            "  不要整段散文。若有圖片請以圖片為準；只有文字提示則合理想像"
            "  並補足缺漏的部位以便生圖。",
            "- 若使用者提示為空，請根據圖片整體風格自行設定。",
            "- companions 是 2~3 個「角色生活圈裡的私人 NPC 配角」"
            "（同事、室友、家人、好友、青梅竹馬…）。這些 NPC 不會自己"
            "出來講話，只是讓角色的日常不再像獨角戲，行程裡可以提到"
            "「跟誰一起做某事」，記憶裡可以提到「跟誰聊過什麼」。",
            "  每個 companion 物件包含五個欄位：",
            "  · name：對方的稱呼（角色腦中怎麼喊他/她），2~10 字。",
            "  · role：與角色的關係（例：室友、同事、表姐、青梅竹馬、學長）。",
            "  · brief_profile：一句話速寫（職業、外貌或標誌性的事），30 字內。",
            "  · personality_sketch：1~3 個短詞描述 NPC 的個性。",
            "  · relationship_snippet：一句話描述「角色 vs 這位 NPC」目前的關係狀態"
            "（例：「兩年室友，感情很好」、「上週才吵過架」），30 字內。",
            "  生出來的 NPC 必須跟角色的人設、生活方式自然吻合 —— 御宅族不會有"
            "「常一起去夜店的朋友」、退休奶奶不會有「同寢的學妹」等。",
            "- 禁止輸出色情、暴力或未成年相關內容。",
        ]
    )


def _build_messages(instruction: str, image: ImageInput | None) -> list[dict[str, Any]]:
    system_msg = {"role": "system", "content": "You are a character design assistant."}
    if image is None:
        return [system_msg, {"role": "user", "content": instruction}]

    data_url = "data:" + image.mime_type + ";base64," + base64.b64encode(image.data).decode("ascii")
    return [
        system_msg,
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]


def _parse_draft(raw: str) -> CharacterDraft | None:
    obj = _extract_object(raw)
    if obj is None:
        return None
    return CharacterDraft(
        name=_coerce_str(obj.get("name"), _MAX_NAME_CHARS),
        name_candidates=_coerce_name_candidates(obj.get("name_candidates")),
        summary=_coerce_str(obj.get("summary"), _MAX_SUMMARY_CHARS),
        personality=_coerce_str_list(obj.get("personality")),
        interests=_coerce_str_list(obj.get("interests")),
        speaking_style=_coerce_str(obj.get("speaking_style"), _MAX_STYLE_CHARS),
        boundaries=_coerce_str_list(obj.get("boundaries")),
        aspirations=_coerce_str_list(obj.get("aspirations")),
        appearance=_coerce_str(obj.get("appearance"), _MAX_APPEARANCE_CHARS),
        gender_identity=_coerce_str(obj.get("gender_identity"), _MAX_IDENTITY_CHARS),
        third_person_pronoun=_coerce_str(
            obj.get("third_person_pronoun"), _MAX_IDENTITY_CHARS,
        ),
        visual_gender_presentation=_coerce_str(
            obj.get("visual_gender_presentation"), _MAX_IDENTITY_CHARS,
        ),
        visual_subject_type=normalise_visual_subject_type(
            obj.get("visual_subject_type"),
        ),
        date_of_birth=_coerce_date(obj.get("date_of_birth")),
        world_frame=_coerce_world_frame(obj.get("world_frame")),
        personality_type=_coerce_personality_type(obj.get("personality_type")),
        companions=_coerce_companions(obj.get("companions")),
    )


def _coerce_name_candidates(value: Any) -> list[CharacterNameCandidate]:
    if not isinstance(value, list):
        return []
    out: list[CharacterNameCandidate] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = _coerce_str(entry.get("name"), _MAX_NAME_CHARS)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(
            CharacterNameCandidate(
                name=name,
                rationale=_coerce_str(
                    entry.get("rationale"), _MAX_NAME_RATIONALE_CHARS,
                ),
            )
        )
        if len(out) >= _MAX_NAME_CANDIDATES:
            break
    return out


def _coerce_personality_type(value: Any) -> CharacterPersonalityType:
    if not isinstance(value, dict):
        return CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]
    try:
        return CharacterPersonalityType.from_payload(value)
    except ValueError:
        return CharacterPersonalityType.DEFAULT  # type: ignore[attr-defined]


def _coerce_companions(value: Any) -> list[CompanionDraft]:
    if not isinstance(value, list):
        return []
    out: list[CompanionDraft] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        name = _coerce_str(entry.get("name"), _MAX_COMPANION_NAME_CHARS)
        if not name:
            continue
        out.append(
            CompanionDraft(
                name=name,
                role=_coerce_str(entry.get("role"), _MAX_COMPANION_ROLE_CHARS),
                brief_profile=_coerce_str(
                    entry.get("brief_profile"), _MAX_COMPANION_PROFILE_CHARS,
                ),
                personality_sketch=_coerce_str_list(
                    entry.get("personality_sketch"),
                ),
                relationship_snippet=_coerce_str(
                    entry.get("relationship_snippet"), _MAX_COMPANION_REL_CHARS,
                ),
            )
        )
        if len(out) >= _MAX_COMPANIONS_IN_DRAFT:
            break
    return out


def _extract_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                try:
                    parsed = json.loads(candidate)
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _coerce_str(value: Any, max_chars: int) -> str:
    if isinstance(value, str):
        return value.strip()[:max_chars]
    return ""


def _coerce_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _coerce_world_frame(value: Any) -> str:
    if not isinstance(value, str):
        return "modern"
    text = value.strip().lower()
    if text in _WORLD_FRAME_VALUES:
        return text
    return "custom" if text else "modern"


def _coerce_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if not isinstance(item, (str, int, float)):
            continue
        text = str(item).strip()[:_MAX_LIST_ITEM_CHARS]
        if text:
            out.append(text)
        if len(out) >= _MAX_LIST_ITEMS:
            break
    return out
