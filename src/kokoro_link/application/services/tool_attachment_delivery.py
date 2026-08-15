"""Turn tool-produced attachments into deliverable outbound attachments.

Tools write files under ``uploads/`` and return **server-relative**
URLs (``/uploads/...``). The web SSE surface can render those as-is,
but Telegram / LINE / Discord fetch the URL themselves — a relative
path is unusable there, so the URL has to be absolutised against the
deployment's public base URL first, and dropped when no such base URL
is configured (better no picture than a broken link the platform will
reject).

Shared by every background surface that runs a tool and then fans the
result out through the proactive delivery path: the proactive decider
(one-pass) and the promise-fulfilment composers (two-pass, see
:mod:`kokoro_link.application.services.composer_tool_loop`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from kokoro_link.contracts.messaging import OutboundAttachment
from kokoro_link.domain.value_objects.tool_call import ToolAttachment

_LOGGER = logging.getLogger(__name__)


def to_outbound_attachments(
    attachments: Iterable[ToolAttachment],
    *,
    public_base_url: str,
    surface: str = "proactive",
) -> tuple[OutboundAttachment, ...]:
    """Absolutise tool attachment URLs for outbound delivery.

    ``surface`` only labels the warning we emit when an attachment has
    to be dropped, so an operator reading logs can tell a proactive
    selfie from a promised one.
    """
    collected: list[OutboundAttachment] = []
    base = (public_base_url or "").rstrip("/")
    for att in attachments:
        url = att.url
        if url.startswith("/"):
            if not base:
                _LOGGER.warning(
                    "dropping %s attachment %s for %s — "
                    "messaging public base URL is not set, external "
                    "platforms cannot fetch a server-relative URL. "
                    "Set Admin Channel settings Public Base URL or "
                    "APP_BASE_URL",
                    surface, url, att.kind,
                )
                continue
            url = f"{base}{url}"
        collected.append(
            OutboundAttachment(
                kind=att.kind,
                url=url,
                mime_type=att.mime_type,
                caption=att.caption,
            ),
        )
    return tuple(collected)


__all__ = ["to_outbound_attachments"]
