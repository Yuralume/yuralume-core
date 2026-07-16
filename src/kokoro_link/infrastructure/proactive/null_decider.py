"""Decider that always declines.

Used with the fake chat provider so the proactive pipeline wires up
end-to-end without generating random noise. Real deployments use
``LLMProactiveDecider``.
"""

from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)


class NullProactiveDecider(ProactiveDeciderPort):
    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        return ProactiveDecision(
            should_send=False,
            reason="null decider — fake provider path",
            message=None,
        )
