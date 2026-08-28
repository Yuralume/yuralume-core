"""LINE dormant-reactivation campaign services (LR series).

Three modules, two of them one per plan phase:

* :mod:`.candidates` (T1) — the read half: which dormant characters the
  console may offer.
* :mod:`.campaigns` (T2) — the send half: the ledger-backed serial runner
  that walks a selection through the ordinary proactive dispatcher under
  the new ``ADMIN_REACTIVATION`` trigger.
* :mod:`.dormancy` — the D1 predicate both halves ask, at the two
  instants that matter (listing time and send time).
"""

from kokoro_link.application.services.line_reactivation.campaigns import (
    DEFAULT_ITEM_CLAIM_LEASE,
    MAX_CAMPAIGN_ID_CHARS,
    MAX_DETAIL_CHARS,
    OUTCOME_SKIPPED_NOT_DORMANT,
    CampaignItemReport,
    CampaignReport,
    CampaignStartResult,
    LineReactivationCampaignService,
    LineReactivationEmptySelectionError,
    LineReactivationInvalidCampaignIdError,
    LineReactivationUnknownCharactersError,
    ProactiveEvaluatorPort,
)
from kokoro_link.application.services.line_reactivation.candidates import (
    DEFAULT_ELIGIBILITY_CONCURRENCY,
    DEFAULT_LISTING_BUDGET_SECONDS,
    ELIGIBILITY_REASON_TRANSIENT,
    LineReactivationCandidateService,
    ReactivationCandidate,
    ReactivationCandidateList,
)
from kokoro_link.application.services.line_reactivation.dormancy import (
    SKIP_REASON_FROZEN,
    SKIP_REASON_NO_LONGER_DORMANT,
    SKIP_REASON_NO_POLICY,
    SKIP_REASON_NOT_FOUND,
    SKIP_REASON_SUBSCRIPTION_LOCKED,
    RecallTargetGuard,
    is_dormant_by_scheduler_rule,
)

__all__ = [
    "DEFAULT_ELIGIBILITY_CONCURRENCY",
    "DEFAULT_ITEM_CLAIM_LEASE",
    "DEFAULT_LISTING_BUDGET_SECONDS",
    "ELIGIBILITY_REASON_TRANSIENT",
    "MAX_CAMPAIGN_ID_CHARS",
    "MAX_DETAIL_CHARS",
    "OUTCOME_SKIPPED_NOT_DORMANT",
    "SKIP_REASON_FROZEN",
    "SKIP_REASON_NOT_FOUND",
    "SKIP_REASON_NO_LONGER_DORMANT",
    "SKIP_REASON_NO_POLICY",
    "SKIP_REASON_SUBSCRIPTION_LOCKED",
    "CampaignItemReport",
    "CampaignReport",
    "CampaignStartResult",
    "LineReactivationCampaignService",
    "LineReactivationCandidateService",
    "LineReactivationEmptySelectionError",
    "LineReactivationInvalidCampaignIdError",
    "LineReactivationUnknownCharactersError",
    "ProactiveEvaluatorPort",
    "ReactivationCandidate",
    "ReactivationCandidateList",
    "RecallTargetGuard",
    "is_dormant_by_scheduler_rule",
]
