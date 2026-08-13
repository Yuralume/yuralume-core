"""Runtime identity of a provider connection row.

``llm`` / ``image`` / ``video`` capabilities all mount their adapters into
a registry keyed by id (``InMemoryChatModelRegistry`` by ``provider_id``,
the image/video profile registries by ``profile.id``). That id used to be
the catalog preset id verbatim, which made "one preset = one live
connection" an invisible constraint: a second row of the same preset
saved fine and showed green in the admin UI while silently replacing the
first one at sync time (rows are synced in ``created_at`` order, so the
newest row won).

Real deployments need more than one row per preset — relay vendors hand
out several API keys with a different model line-up behind each, and one
``custom_openai_compatible`` row cannot express that.

So a row's runtime id is derived here, in ONE place shared by the
builders, the sync and the save-time uniqueness check:

* blank slug → the preset id verbatim. Existing installs keep their ids,
  so stored ``active_model`` / ``feature_models`` / per-character
  overrides / NSFW targets keep resolving after the upgrade.
* non-blank slug → ``<preset>__<slug>``, unique per row.

``tts`` / ``embedding`` / ``search`` deliberately stay out of
:data:`IDENTITY_SCOPED_CAPABILITIES`: those mount exactly one backend
(most-recently-updated row wins), so several rows of one preset is their
documented active/standby behaviour rather than a collision.
"""

from __future__ import annotations

import re
from typing import Any, Protocol


class ProviderRowLike(Protocol):
    """Structural view of what deriving a runtime id needs.

    Both the persisted ``ProviderConnection`` and the redacted
    ``ProviderConnectionView`` the admin/sync paths pass around satisfy
    it, so neither side has to convert just to ask for an id.
    """

    provider: str
    config: dict[str, Any]

#: Catalog config field carrying the operator-chosen slug.
CONNECTION_SLUG_FIELD_KEY = "connection_slug"

#: Capabilities whose runtime registry is keyed by id, i.e. where two rows
#: of one preset need distinct ids to coexist.
IDENTITY_SCOPED_CAPABILITIES = frozenset({"llm", "image", "video"})

#: Separator between preset id and slug. Double underscore because a
#: normalised slug never contains one (``_`` is folded to ``-`` below), so
#: the boundary stays unambiguous.
RUNTIME_ID_SEPARATOR = "__"

SLUG_MAX_LENGTH = 32

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def normalize_connection_slug(raw: object) -> str:
    """Fold operator input into ``[a-z0-9-]`` (empty when nothing survives).

    Non-ASCII input collapses to the empty string rather than producing a
    unicode runtime id that would then travel through URL paths, logs and
    stored preferences. Callers that can reject (the save path) turn an
    empty result from non-empty input into an explicit error; the runtime
    treats it as "no slug" so a stored row can never fail to build.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    collapsed = _NON_SLUG_CHARS.sub("-", text).strip("-")
    return collapsed[:SLUG_MAX_LENGTH].strip("-")


def runtime_provider_id(row: ProviderRowLike) -> str:
    """The id this row occupies in the registries it mounts into."""
    slug = normalize_connection_slug(row.config.get(CONNECTION_SLUG_FIELD_KEY))
    if not slug:
        return row.provider
    return f"{row.provider}{RUNTIME_ID_SEPARATOR}{slug}"


def preset_id_of(runtime_id: str) -> str:
    """The catalog preset a runtime id belongs to (inverse of the join).

    Used by display code that wants the preset's localized name for a
    slugged id. A preset id never contains ``__`` itself, so splitting on
    the first occurrence is exact.
    """
    return runtime_id.split(RUNTIME_ID_SEPARATOR, 1)[0]
