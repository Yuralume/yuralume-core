"""Public showcase pipeline.

Core's contribution to the Cloud portal's public wall: read the
character's own posts, filter the structurally unpublishable, advise on
the rest with an LLM pre-review, translate what the owner approved, and
render the snapshot document. Approval and storage belong to the Cloud
control plane — see
:mod:`kokoro_link.application.services.showcase.service`.
"""

from kokoro_link.application.services.showcase.filters import (
    FilterOutcome,
    PublicActivity,
    ShowcaseCandidate,
    is_public_activity,
    mechanical_filter,
    select_public_activities,
)
from kokoro_link.application.services.showcase.review import (
    VERDICT_FLAG,
    VERDICT_NEEDS_MANUAL_REVIEW,
    VERDICT_PASS,
    ReviewSummary,
    ShowcaseReview,
    ShowcaseReviewer,
)
from kokoro_link.application.services.showcase.service import (
    DEFAULT_POST_LIMIT,
    CandidateBundle,
    ShowcaseError,
    ShowcaseNotFound,
    ShowcaseService,
    TranslationRequest,
    TranslationResult,
    build_showcase_service,
    post_source_hash,
)
from kokoro_link.application.services.showcase.snapshot import (
    DEFAULT_LOCALES,
    SOURCE_LOCALE,
    CharacterCard,
    SnapshotError,
    SnapshotPost,
    SnapshotResult,
    build_snapshot,
)
from kokoro_link.application.services.showcase.translate import ShowcaseTranslator

__all__ = [
    "DEFAULT_LOCALES",
    "DEFAULT_POST_LIMIT",
    "SOURCE_LOCALE",
    "VERDICT_FLAG",
    "VERDICT_NEEDS_MANUAL_REVIEW",
    "VERDICT_PASS",
    "CandidateBundle",
    "CharacterCard",
    "FilterOutcome",
    "PublicActivity",
    "ReviewSummary",
    "ShowcaseCandidate",
    "ShowcaseError",
    "ShowcaseNotFound",
    "ShowcaseReview",
    "ShowcaseReviewer",
    "ShowcaseService",
    "ShowcaseTranslator",
    "SnapshotError",
    "SnapshotPost",
    "SnapshotResult",
    "TranslationRequest",
    "TranslationResult",
    "build_showcase_service",
    "build_snapshot",
    "is_public_activity",
    "mechanical_filter",
    "post_source_hash",
    "select_public_activities",
]
