from pydantic import BaseModel, Field

from kokoro_link.contracts.scene_access import (
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.value_objects.presence_frame import AccessContext


class StageAccessVerdictResponse(BaseModel):
    decision: StageAccessDecision
    recommended_action: StageAccessAction
    access_context: AccessContext
    reason_for_user: str = Field(min_length=1)
    prompt_fact: str = Field(min_length=1)
    suggested_opener: str | None = None

    @classmethod
    def from_domain(
        cls, verdict: StageAccessVerdict,
    ) -> "StageAccessVerdictResponse":
        return cls(
            decision=verdict.decision,
            recommended_action=verdict.recommended_action,
            access_context=verdict.access_context,
            reason_for_user=verdict.reason_for_user,
            prompt_fact=verdict.prompt_fact,
            suggested_opener=verdict.suggested_opener,
        )
