"""Shared text helpers for prompt sections."""


LATEST_USER_MESSAGE_MARKER = "最新使用者訊息："
"""Marker used by ``FakeChatModel`` to locate the latest user message.

Exposed as a module constant so callers that need to parse the rendered
prompt (tests, fake provider) do not hard-code the string.
"""


def _clip(value: str, limit: int) -> str:
    text = " ".join((value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


_DIGEST_SOURCE_FRAME = (
    "以下是事實參照，不是文體範本；不要模仿其措辭、句式或意象。"
)
"""Shared preface for every block that hands the model *material* rather
than voice: digests, emotion events and reflections all quote raw source
text, and without this line the model imitates its register."""
