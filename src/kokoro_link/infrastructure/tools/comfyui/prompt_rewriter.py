"""LLM-backed ``PromptRewriterPort`` implementation.

Calls the configured chat model with a narrow, system-style
instruction: "take this description, give me danbooru-style tags,
nothing else". The returned text is sanitised (strip code fences,
surrounding quotes, leading ``positive:`` labels) before being
returned; we're strict here so one misbehaving model turn can't
inject paragraphs of reasoning into the final image prompt.

Failure modes — LLM timeout, empty output, non-ASCII-heavy output
that suggests the model didn't translate — raise
``PromptRewriteError``. ``ComfyPortraitGenerator`` catches and falls
back to the raw input so image generation still proceeds.
"""

from __future__ import annotations

import logging
import re

from collections.abc import Sequence
from typing import TYPE_CHECKING

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.prompt_rewriter import (
    PromptRewriteError,
    PromptRewriterPort,
)

if TYPE_CHECKING:
    from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a Stable Diffusion prompt assistant for Illustrious XL "
    "(danbooru-tag-trained SDXL model).\n"
    "The input may be written in any language (中文 / English / 自然語言 / "
    "已是 tag 都可) and contains some or all of:\n"
    "  - Character appearance (stable identity baseline)\n"
    "  - Character gender identity and visual gender presentation "
    "(explicit identity facts)\n"
    "  - Visual subject type / species / body-plan rules for media "
    "generation\n"
    "  - Current mood\n"
    "  - Current activity (what the character is doing right now)\n"
    "  - Scene description (pose, action, location, lighting)\n\n"
    "## Your job\n"
    "Produce ONE unified line of English danbooru-style tags that "
    "depicts the character in the current scene/activity. The SCENE "
    "is what's being drawn — identity tags exist to support it, not "
    "override it.\n\n"
    "## Tag layering (priority order)\n"
    "0. Visual subject type / body plan (ALWAYS obey first): if the "
    "input says 'non-human animal', render a real animal of that "
    "species/body plan. Use animal tags such as 'cat', 'dog', "
    "'animal focus', 'no humans', 'quadruped' when applicable. Do NOT "
    "use human count tags like '1girl'/'1boy' for non-human animals.\n"
    "1. Stable identity (ALWAYS keep): hair colour/length/style, eye "
    "colour, skin tone, body type, character gender identity, visual "
    "gender presentation, ethnic features. Example: '1girl, long "
    "black hair, green eyes' when the facts support feminine "
    "presentation; use '1boy', 'androgynous', 'gender-neutral', or "
    "similar tags when the explicit visual facts say so.\n"
    "2. Default wardrobe: outfit mentioned in 'Character appearance'. "
    "Keep it UNLESS one of the conflict-resolution conditions below "
    "applies — INCLUDING the 'User reference image' rule. The "
    "appearance line typically mixes identity tags (hair, eyes) with "
    "wardrobe tags (uniform, ribbon, skirt). On a wardrobe-override "
    "turn you keep the identity half and DROP the wardrobe half — do "
    "NOT carry both the original outfit and the new one.\n"
    "3. Accessories / held items from appearance (wand, book, sword, "
    "glasses, cat ears): DROP these unless the scene makes sense for "
    "them. A character who 'holds a wand' in their appearance should "
    "NOT be holding it while sleeping, bathing, eating, or in any "
    "scene that doesn't call for it.\n"
    "4. Mood → facial expression tag (e.g. 'gentle smile', 'tired "
    "expression'). If the scene implies a different expression "
    "('crying', 'sleeping'), the scene wins.\n"
    "5. Scene-specific tags: pose, action, location, lighting, "
    "camera angle. These always make it in.\n\n"
    "## Conflict resolution (critical)\n"
    "- Gender / presentation facts are explicit facts. Do NOT infer "
    "visual gender from the character name, pronoun, summary, or "
    "product copy. If Visual gender presentation is set, preserve it "
    "as the visual anchor even when nearby wording uses another "
    "pronoun. If it is unset, rely on Character appearance as written "
    "and do not invent a binary gender tag.\n"
    "- **Visual subject type: non-human animal** → the character is NOT "
    "a person. Preserve species and animal anatomy. Output 'no humans' "
    "and concrete animal tags. Do NOT output 1girl, 1boy, man, woman, "
    "person, human face, human body, human hands, cat ears on a human, "
    "furry humanoid body, or anthro humanoid body unless the subject type "
    "explicitly says anthropomorphic.\n"
    "- **Visual subject type: anthropomorphic animal / furry** → a "
    "humanoid animal body is intentional. Use anthropomorphic/furry "
    "tags and species traits; do not collapse it into an ordinary "
    "human portrait.\n"
    "- **Visual subject type: object / non-living mascot** → render the "
    "object as itself; do not add a human face or body unless "
    "Appearance explicitly says so.\n"
    "- **User reference image attached** → the image is the SOURCE "
    "OF TRUTH for outfit/clothing/props/pose/location for THIS shot. "
    "DROP every wardrobe-related tag from 'Character appearance' "
    "(uniforms, dresses, skirts, ribbons, shoes, accessories) and "
    "REPLACE them with concrete danbooru wardrobe tags read off the "
    "image. Keep only identity tags (hair, eyes, body) from "
    "appearance. This rule fires whenever an image is attached — you "
    "do NOT need the scene text to explicitly say '換上' / 'wear "
    "this' for it to apply; the user attached the image because they "
    "want it applied.\n"
    "- Sleeping / 睡覺 / 躺在床上 / resting eyes / meditating → "
    "ADD 'closed eyes' and OMIT any eye-open tag. Character does "
    "NOT hold items in this state.\n"
    "- Bathing / 洗澡 / swimming / in bathtub / 泡湯 → DROP the "
    "default outfit tag; use appropriate scene-specific clothing "
    "or skin tags (e.g. 'bathing', 'wet hair'). No held items.\n"
    "- Sports / running / 運動 / fighting → DROP formal wear and "
    "non-sport accessories; no held items unless the scene names "
    "one (e.g. 'holding tennis racket').\n"
    "- Eating / cooking / 吃東西 / 做菜 → hands are busy; drop "
    "appearance-level held items; may add food/utensil tags from "
    "scene.\n"
    "- Intimate / 親密 / in bed with partner → simplify outfit per "
    "scene; drop unrelated accessories.\n"
    "- Strong emotion in scene ('crying', 'laughing', 'screaming') "
    "overrides the mood tag.\n\n"
    "## User-attached reference image(s) — detailed protocol\n"
    "When the input contains 'User reference image(s) attached this "
    "turn', the message ALSO carries one or more images visible to "
    "you. Read them concretely and apply the override above. Specific "
    "rules:\n"
    "  - Clothing / outfit: extract every visible garment as danbooru "
    "wardrobe tags (e.g. 'white blouse, plaid skirt, knee socks, "
    "loafers'). These REPLACE the appearance's default outfit — do "
    "NOT keep the appearance wardrobe alongside the new one, the "
    "result would be a contradictory mash-up (e.g. 'school uniform, "
    "white blouse, plaid skirt' is WRONG; just emit 'white blouse, "
    "plaid skirt' if that's what the image shows).\n"
    "  - Pose / location: if the image shows a setting or pose, "
    "extract concrete tags (e.g. 'sitting on bench, park, sunset').\n"
    "  - Identity baseline: hair colour, eye colour, body type, "
    "ethnic features, gender identity, and visual gender presentation "
    "STILL come from the character identity facts. The "
    "reference image does NOT change who the character is — only "
    "what they wear, where they are, how they pose.\n"
    "  - Disagreement between image and scene: image wins on visual "
    "details (clothing, location, lighting); scene text wins on "
    "action / intent / mood (what the character is doing, how they "
    "feel). Example: image shows a white dress, scene says '在咖啡店 "
    "微笑著閱讀' → output 'white dress, cafe, sitting, reading book, "
    "gentle smile' — wardrobe from image, action+mood from scene.\n\n"
    "## Handling figurative / flowery input (important)\n"
    "Chat LLMs sometimes hand us metaphors or mood-paint rather than "
    "concrete scene facts, e.g. '心情像春日微風' or 'in a moment of "
    "tender vulnerability'. Translate these to concrete, literal "
    "danbooru tags — extract the observable referent, don't preserve "
    "the metaphor:\n"
    "  - '心情像春日微風般溫暖' → 'gentle smile, relaxed expression'\n"
    "  - 'tender vulnerability' → 'soft expression, looking down'\n"
    "  - '眼神像被寒意穿透' → 'cold expression, sharp gaze'\n"
    "  - '沐浴在金色陽光中' → 'warm lighting, sunlight'\n"
    "If the input is purely abstract ('ineffable feeling', "
    "'nameless longing') with no concrete scene to draw, fall back "
    "to a plain subject-appropriate portrait. For a human subject this "
    "may be '1girl, neutral expression, simple background'; for a "
    "non-human animal use 'no humans, animal focus, neutral pose, "
    "simple background'. Never invent speculative scene details the "
    "input didn't hint at.\n\n"
    "## Output rules (strict)\n"
    "- Output ONLY the comma-separated tags. No explanation, no "
    "prefix like 'positive:' or 'prompt:', no code fences, no quotes.\n"
    "- Translate non-English input (e.g. 長直黑髮 → 'long black "
    "hair, straight hair'; 碧綠的眼眸 → 'green eyes').\n"
    "- Keep total under ~30 tags; prune low-value ones if you're "
    "over.\n"
    "- Use danbooru vocabulary ('1girl', 'long black hair', 'red "
    "ribbon', 'sailor uniform', 'cafe', 'sitting', 'reading book', "
    "'soft lighting'). For non-human animals use animal vocabulary "
    "instead of human count tags.\n"
    "- DO NOT include quality boosters (masterpiece, best quality, "
    "etc.) — added separately.\n"
    "- DO NOT include negative-style tags (lowres, bad anatomy, etc.)."
)

_USER_TEMPLATE = "{text}\n\nDanbooru tags:"

_MIN_OUTPUT_CHARS = 3
_MAX_OUTPUT_CHARS = 800


class LLMPromptRewriter(PromptRewriterPort):
    def __init__(
        self,
        *,
        model: ChatModelPort | None = None,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def rewrite(
        self,
        text: str,
        *,
        character: "Character | None" = None,
        image_urls: Sequence[str] = (),
    ) -> str:
        source = text.strip()
        if not source:
            return ""
        if await self._resolver.is_fake(character=character):
            # Fake backend can't translate Chinese → danbooru tags; just
            # pass the original through so generation continues (with
            # predictably poor image quality — but no hard failure).
            return source[:_MAX_OUTPUT_CHARS]
        prompt = f"{_SYSTEM_PROMPT}\n\n{_USER_TEMPLATE.format(text=source)}"
        urls = tuple(u for u in (image_urls or ()) if u)
        # Explicit INPUT log — what the rewriter LLM is about to see.
        # Logged at INFO so an operator running ``--log-level info`` can
        # eyeball the structured payload + image refs without flipping
        # to DEBUG. ``_describe_image_urls`` collapses data: URLs to
        # their MIME + byte count so a 5 MB base64 blob doesn't drown
        # the log line.
        _LOGGER.info(
            "prompt rewriter INPUT image_count=%d images=%s\n"
            "--- payload begin ---\n%s\n--- payload end ---",
            len(urls), _describe_image_urls(urls), source,
        )
        try:
            # ``image_urls`` is forwarded as a kwarg straight through
            # ``ModelResolver.generate`` → ``ChatModelPort.generate``.
            # Vision-capable providers attach the images; non-vision
            # providers ignore the kwarg silently. Only forward when
            # we actually have images so legacy fake/mock backends
            # (whose ``generate`` may not accept the kwarg) still work
            # on the no-image path.
            extra: dict = {"image_urls": urls} if urls else {}
            raw = await self._resolver.generate(
                prompt, character=character, **extra,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("prompt rewriter LLM call failed")
            raise PromptRewriteError(f"LLM call failed: {exc}") from exc

        cleaned = _clean(raw)
        # Explicit OUTPUT log — paired with the INPUT log above. We log
        # both ``raw`` and ``cleaned`` because the cleanup pass strips
        # code fences / labels / quotes, and the difference is useful
        # when debugging why the final image looks off ("did the LLM
        # actually emit those tags, or did cleanup eat half of them?").
        _LOGGER.info(
            "prompt rewriter OUTPUT raw=%r cleaned=%r", raw, cleaned,
        )
        if len(cleaned) < _MIN_OUTPUT_CHARS:
            raise PromptRewriteError(
                f"rewritten prompt too short: {cleaned!r}",
            )
        if len(cleaned) > _MAX_OUTPUT_CHARS:
            cleaned = cleaned[:_MAX_OUTPUT_CHARS].rstrip().rstrip(",")
        return cleaned


def _describe_image_urls(urls: Sequence[str]) -> str:
    """Render an image URL list compactly for log lines.

    ``data:`` URLs get collapsed to ``data:<mime>;base64,<N bytes>``
    so a 5 MB inlined photo doesn't bloat the log. HTTP(S) URLs pass
    through (already short). Returns ``"[]"`` for an empty list so
    grep'ing the log for ``images=[]`` finds the no-image turns.
    """
    if not urls:
        return "[]"
    rendered: list[str] = []
    for u in urls:
        if u.startswith("data:"):
            head, _, payload = u.partition(",")
            rendered.append(f"{head},<{len(payload)} bytes>")
        else:
            rendered.append(u)
    return "[" + ", ".join(rendered) + "]"


_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_LABEL_RE = re.compile(
    r"^(?:positive|prompt|tags|danbooru\s*tags?)\s*[:：]\s*",
    re.IGNORECASE,
)


def _clean(raw: str) -> str:
    """Strip code fences, labels, surrounding quotes, stray newlines.

    Models sometimes wrap the answer like
    ``positive: "1girl, cafe, ..."`` or as a fenced block. We flatten
    all of that so the caller gets a clean single-line tag list it
    can concatenate with the quality boilerplate + character
    appearance without re-parsing.
    """
    text = raw.strip()
    # Drop code fences wherever they appear.
    text = _FENCE_RE.sub("", text)
    text = text.replace("```", "")
    # First non-empty line is the answer; extra reasoning below gets
    # discarded (models sometimes tack on an explanation despite
    # being told not to).
    for line in text.splitlines():
        candidate = line.strip()
        if candidate:
            text = candidate
            break
    else:
        text = ""
    # Strip leading labels ("positive:", "tags:", etc.).
    text = _LABEL_RE.sub("", text).strip()
    # Strip outer quotes.
    if len(text) >= 2 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    # Collapse runs of whitespace around commas.
    text = re.sub(r"\s*,\s*", ", ", text).strip(", ").strip()
    return text
