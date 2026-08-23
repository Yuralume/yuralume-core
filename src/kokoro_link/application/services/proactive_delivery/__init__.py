"""External proactive delivery adapters (LH4, §8.3).

The ``ProactiveDispatcher`` routes only its *external* sink through
:class:`~kokoro_link.contracts.external_proactive.ExternalProactiveDeliveryPort`;
its web / history / SSE sink stays in the dispatcher. This package holds the
self-host adapter (:class:`LocalMessagingProactiveDeliveryAdapter`) that wraps
the existing account / binding repositories + platform adapters, the Hosted
Channel adapter (:class:`HostedChannelProactiveDeliveryAdapter`) that carries the
envelope to the Cloud Channel Hub, and the pre-send ledger retry worker
(:class:`ProactiveDeliveryRetryWorker`) that re-sends still-pending events.
"""

from kokoro_link.application.services.proactive_delivery.eligible_binding import (
    ResolvedProactiveSink,
    find_eligible_proactive_binding,
    list_eligible_proactive_bindings,
)
from kokoro_link.application.services.proactive_delivery.hosted_adapter import (
    HostedChannelProactiveDeliveryAdapter,
)
from kokoro_link.application.services.proactive_delivery.hosted_identity import (
    build_hosted_delivery_identity_resolver,
)
from kokoro_link.application.services.proactive_delivery.local_adapter import (
    LocalMessagingProactiveDeliveryAdapter,
)
from kokoro_link.application.services.proactive_delivery.retry_worker import (
    ProactiveDeliveryRetryWorker,
)

__all__ = [
    "HostedChannelProactiveDeliveryAdapter",
    "LocalMessagingProactiveDeliveryAdapter",
    "ProactiveDeliveryRetryWorker",
    "ResolvedProactiveSink",
    "build_hosted_delivery_identity_resolver",
    "find_eligible_proactive_binding",
    "list_eligible_proactive_bindings",
]
