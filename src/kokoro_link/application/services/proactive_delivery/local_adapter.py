"""Self-host proactive delivery adapter (LH4, §8.3).

Wraps the existing self-host messaging path — the ``account`` / ``binding``
repositories and the per-platform channel adapters — behind
:class:`~kokoro_link.contracts.external_proactive.ExternalProactiveDeliveryPort`.
Behaviour is byte-identical to the dispatcher's former ``_deliver``: resolve the
eligible binding, self-heal / create its ``source=<platform>`` conversation,
append the assistant turn, and hand the segmented outbound to the platform
adapter. This is the default (and, for this batch, only) adapter the
composition root wires; the Hosted Channel adapter slots in behind the same port
in a later batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.application.services.outbound_message_segments import (
    send_segmented_outbound,
)
from kokoro_link.application.services.proactive_delivery.eligible_binding import (
    find_eligible_proactive_binding,
)
from kokoro_link.contracts.external_proactive import (
    DeliveryAcceptance,
    DeliveryAcceptanceStatus,
    DeliveryEligibility,
    ExternalProactiveDeliveryPort,
    ProactiveEnvelope,
)
from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
    OutboundMessage,
)
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.platform import Platform

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LocalDeliveryTarget:
    """The exact ``(binding, account)`` the dispatcher's gate resolved.

    L12: passing this snapshot into :meth:`accept` pins the send to the target
    the gate authorised, so a binding that is re-pointed / re-eligibilised
    between the gate check and delivery can never silently redirect the push.
    When absent (e.g. the ledger retry worker, which re-derives from the stored
    envelope) the adapter falls back to resolving the eligible binding itself.
    """

    binding: ChannelBinding
    account: MessagingAccount


class LocalMessagingProactiveDeliveryAdapter(ExternalProactiveDeliveryPort):
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        binding_repository: ChannelBindingRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        adapters: dict[Platform, ChannelAdapterPort],
    ) -> None:
        self._accounts = account_repository
        self._bindings = binding_repository
        self._conversations = conversation_repository
        # Keyed by platform *value* to match the dispatcher's former lookup.
        self._adapters = {p.value: a for p, a in adapters.items()}

    async def check_eligibility(
        self, character_id: str,
    ) -> DeliveryEligibility:
        eligible = await find_eligible_proactive_binding(
            account_repository=self._accounts,
            binding_repository=self._bindings,
            character_id=character_id,
        )
        if eligible is None:
            return DeliveryEligibility(
                eligible=False,
                reason=(
                    "no binding has accepts_proactive=True"
                ),
            )
        return DeliveryEligibility(eligible=True)

    async def accept(
        self,
        envelope: ProactiveEnvelope,
        *,
        target: object | None = None,
    ) -> DeliveryAcceptance:
        # L12: prefer the gate-resolved snapshot verbatim; only re-resolve when
        # the caller has no snapshot to hand (retry worker rehydrating from the
        # ledger).
        if isinstance(target, LocalDeliveryTarget):
            binding, account = target.binding, target.account
        else:
            eligible = await find_eligible_proactive_binding(
                account_repository=self._accounts,
                binding_repository=self._bindings,
                character_id=envelope.character_id,
            )
            if eligible is None:
                return DeliveryAcceptance(
                    status=DeliveryAcceptanceStatus.TERMINAL,
                    reason="no eligible proactive binding",
                )
            binding, account = eligible
        await self._deliver(
            binding=binding,
            account=account,
            text=envelope.text,
            attachments=envelope.attachments,
            locale=envelope.locale,
        )
        return DeliveryAcceptance(
            status=DeliveryAcceptanceStatus.ACCEPTED,
            delivery_id=binding.id,
        )

    async def _deliver(
        self,
        *,
        binding: ChannelBinding,
        account: MessagingAccount,
        text: str,
        attachments,
        locale: str,
    ) -> None:
        conversation_id = binding.conversation_id
        updated_binding = binding
        if conversation_id is None:
            conversation = Conversation.start(
                character_id=account.character_id,
                source=account.platform.value,
            )
            await self._conversations.save(conversation)
            updated_binding = binding.with_conversation(conversation.id)
            await self._bindings.save(updated_binding)
            conversation_id = conversation.id

        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            conversation = Conversation.start(
                character_id=account.character_id,
                source=account.platform.value,
            )
            await self._conversations.save(conversation)
            updated_binding = updated_binding.with_conversation(conversation.id)
            await self._bindings.save(updated_binding)
        appended = conversation.append(
            Message(role=MessageRole.ASSISTANT, content=text),
        )
        await self._conversations.save(appended)

        adapter = self._adapters.get(account.platform.value)
        if adapter is None:
            raise RuntimeError(
                f"no adapter registered for platform {account.platform.value}",
            )
        await send_segmented_outbound(
            adapter,
            OutboundMessage(
                platform=account.platform,
                chat_ref=updated_binding.chat_ref,
                text=text,
                credentials=account.credentials,
                attachments=attachments,
                locale=locale,
            ),
        )
