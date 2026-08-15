"""Port for the tier-gated official-card shelf (TG series).

A tier-locked official card is one Cloud has fenced to a single tenant tier
— a work-in-progress character being tested in the real hosted environment
before it opens to everyone (plan D1). To every anonymous reader such a card
**does not exist**: it is absent from the public catalog, its detail is a
404, and its artifact is a 404. So the public
:class:`~kokoro_link.contracts.official_card_catalog.OfficialCardCatalogPort`
cannot answer for it at all, and this is the second shelf that can.

Why it is a separate port rather than a parameter on the first one:

* **The other one is anonymous, and that is a promise.** Every read there
  carries no credential and no deployment id. This route carries the
  ``core_to_user`` service credential *and* names the tenant it is asking
  on behalf of — the opposite property, and folding the two together would
  put the identifying read one forgotten argument away from the anonymous
  one.
* **The other one caches, and this one must not** (plan D6). The public
  catalog cache is one shelf shared by every player in the process;
  per-tier rows dropped into it would be served to the next reader whose
  tier does not include them. These rows are read per request, and the
  volume that makes that acceptable — a handful of cards, a handful of
  testers — is the same fact that makes a per-tier cache not worth its
  invalidation.

Two properties are contractual:

* **Fail-soft is an empty shelf, never a wall.** ``None`` means "we could
  not find out", which every caller renders the same way it renders a
  catalog outage: the tier-locked rows are simply not there and the rest of
  the gallery is untouched. A tester who cannot see the WIP card for a
  minute is a far better failure than a gallery that will not load.
* **The tenant is named, the tier is not.** Core holds a cached
  ``cloud_tenant_tier`` on the operator profile and deliberately does not
  send it: a cached tier drifts, and the source of truth for which tier a
  tenant is in lives on the Cloud side of this call (plan D4). What travels
  is the tenant id; Cloud resolves the tier itself, every time.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from kokoro_link.contracts.official_card_catalog import OfficialCardDetail


class OfficialCardGatedCatalogPort(ABC):
    """Read-only access to the official cards one tenant's tier may see."""

    @abstractmethod
    async def list_gated_cards(
        self, *, tenant_id: str, locale: str,
    ) -> list[OfficialCardDetail] | None:
        """Every tier-locked card visible to ``tenant_id``, or ``None``.

        The rows come back as full
        :class:`~kokoro_link.contracts.official_card_catalog.OfficialCardDetail`
        documents rather than catalog summaries because they are the *only*
        document these cards have: there is no anonymous detail read to
        follow up with, so browse, card face and install all have to be
        served from what this one call returns.

        They are masked exactly as an anonymous read of a cloud-exclusive
        card is — no prose, no artifact URL — so a row reports
        ``cloud_exclusive`` and installs through the authenticated payload
        endpoint, which is the only path that exists for it.

        **An empty list is a real answer.** A tenant whose tier has no cards
        fenced to it, and a deployment whose player has no Cloud tenant at
        all, both legitimately see nothing here. Only ``None`` means the
        question could not be asked.
        """


__all__ = ["OfficialCardGatedCatalogPort"]
