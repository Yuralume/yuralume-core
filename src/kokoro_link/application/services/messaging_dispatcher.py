"""Inbound messaging dispatcher.

Inbound flow (platform-agnostic once the adapter has normalised payload):

1. Debounce by ``platform_message_id`` — webhook retries don't double-fire.
   Two layers: the process-local ``InboundDebouncer`` as a cheap front gate,
   then the durable ``InboundReceiptPort`` claim so a retry the load balancer
   lands on a *different* api replica can't re-run the whole turn
2. Load the ``MessagingAccount`` referenced by ``message.account_id``;
   drop silently if gone / disabled
3. Apply the account's ``allowed_sender_refs`` allowlist — anything
   outside gets dropped so stray DMs can't pollute the character's
   memory. Empty allowlist means accept everyone (convenient during
   first-bind; operators should lock it down after).
4. Find (or lazily create) the ``ChannelBinding`` for this (account,
   chat_ref). First contact from a chat spawns a fresh ``Conversation``
   tagged with the account's platform as ``source``; the id is written
   back so the same chat keeps the same thread.
5. Run ``ChatService.send_message`` — exactly the same pipeline the
   web UI uses, so character state / memory / goals / schedule stay
   coherent across every surface.
6. Hand the reply to the platform's adapter, passing the account's
   credentials per-call.

No platform-specific logic lives here — adapters handle that upstream
(webhook parsing) and downstream (REST calls to the platform).

**Conversation-busy rollback.** Step 5 runs under the per-conversation turn
lease, which rejects a delivery that overlaps a sibling turn. That rejection
happens before anything is written, so it must not consume the delivery: after
a bounded retry the dispatcher gives back *both* dedup stamps (durable receipt
and in-process debounce) and re-raises, leaving the transport free to
re-deliver. Every other failure keeps the historical semantics — swallowed,
stamps retained — because a turn that crashed mid-flight may already have
appended the user message and charged for it.

**Drain rollback (GD1-A).** A replica told to drain refuses new turns on the
first line of ``ChatService._begin_turn``, which puts a drained delivery in
exactly the conversation-busy shape: the claim was taken in step 1, nothing was
written, and the refusal is about *this replica*, not about the message. So it
is handed back the same way. Letting the generic ``except`` below swallow it
would be the worst possible outcome — the receipt stays stamped, the webhook
answers 200, the platform never retries, and the player's message is gone for
good with no error anywhere.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import Enum

from kokoro_link.application.dto.chat import PresenceFramePayload, SendChatMessageRequest
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.application.services.drain_state import ServerDrainingError
from kokoro_link.application.services.persona_disclosure_gate import (
    persona_safe_for_account,
)
from kokoro_link.application.services.outbound_message_segments import (
    send_segmented_outbound,
)
from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    ChannelBindingRepositoryPort,
    InboundMessage,
    MessagingAccountRepositoryPort,
    OutboundAttachment,
    OutboundMessage,
)
from kokoro_link.contracts.inbound_receipts import InboundReceiptPort
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame
from kokoro_link.infrastructure.localization import resolve_fallback_language
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    localize_inbound_placeholder_text,
)

_LOGGER = logging.getLogger(__name__)


DEFAULT_BUSY_RETRY_ATTEMPTS = 3
"""Total ``send_message`` attempts before a busy conversation is handed back.

Only worth a small budget: the sibling turn holding the lease is an LLM turn
that usually runs for tens of seconds, so retrying here just catches the tail
of one that is about to finish. The real recovery is the transport's
re-delivery — but the Discord / WhatsApp gateways have no re-delivery at all,
which is why the budget is not zero."""

DEFAULT_BUSY_RETRY_DELAY_SECONDS = 1.5
"""Pause between busy attempts. Kept short: the webhook routes run the whole
turn inside the request handler, so this delay is spent against the platform's
own request timeout."""


class _ClaimOutcome(Enum):
    """What the durable receipt claim decided about this delivery."""

    OWNED = "owned"
    """This caller inserted the receipt — and is therefore the only one
    allowed to release it again."""

    DUPLICATE = "duplicate"
    """Another instance owns the delivery; drop it."""

    DEGRADED = "degraded"
    """The ledger is unwired or unreachable. Proceed on the in-process
    debouncer alone, and never release a row we cannot prove we wrote."""


class MessagingDispatcher:
    def __init__(
        self,
        *,
        account_repository: MessagingAccountRepositoryPort,
        binding_repository: ChannelBindingRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        chat_service: ChatService,
        adapters: dict[Platform, ChannelAdapterPort],
        debouncer: InboundDebouncer | None = None,
        receipt_repository: InboundReceiptPort | None = None,
        busy_retry_attempts: int = DEFAULT_BUSY_RETRY_ATTEMPTS,
        busy_retry_delay_seconds: float = DEFAULT_BUSY_RETRY_DELAY_SECONDS,
        public_base_url: str = "",
        public_base_url_provider: Callable[[], Awaitable[str]] | None = None,
        operator_language_resolver: (
            Callable[[str], Awaitable[str]] | None
        ) = None,
    ) -> None:
        self._accounts = account_repository
        self._bindings = binding_repository
        self._conversations = conversation_repository
        self._chat = chat_service
        self._adapters = {p.value: a for p, a in adapters.items()}
        self._debouncer = debouncer
        # Durable cross-instance twin of the debouncer. ``None`` on the
        # no-DB / in-memory path, where behaviour stays exactly what it was:
        # a single process is fully covered by the debouncer alone.
        self._receipts = receipt_repository
        self._busy_retry_attempts = max(1, busy_retry_attempts)
        self._busy_retry_delay_seconds = max(0.0, busy_retry_delay_seconds)
        # Relative URLs (``/v1/public/...``) become absolute before
        # delivery to external platforms or adapters that self-fetch
        # before upload. Empty base_url means "don't rewrite", which
        # suits local dev without public channel delivery.
        self._public_base_url = public_base_url.rstrip("/")
        self._public_base_url_provider = public_base_url_provider
        # Resolves a character's owning-operator content language (BCP 47)
        # so we can (a) localize the zh-TW inbound attachment placeholder
        # stored as the user turn text, and (b) tag the outbound message
        # locale for channel-wrapper localization. ``None`` → ship-first
        # ``zh-TW`` (the prior behaviour).
        self._operator_language_resolver = operator_language_resolver

    async def handle_inbound(self, message: InboundMessage) -> None:
        if self._debouncer is not None and self._debouncer.should_drop(message):
            _LOGGER.debug(
                "dropping duplicate inbound %s/%s id=%s",
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )
            return

        claim = await self._claim_delivery(message)
        if claim is _ClaimOutcome.DUPLICATE:
            return

        account = await self._accounts.get(message.account_id)
        if account is None:
            _LOGGER.info(
                "inbound references missing account %s; ignoring",
                message.account_id,
            )
            return
        if not account.enabled:
            return
        if not account.is_sender_allowed(message.sender_ref):
            _LOGGER.info(
                "dropping inbound from unauthorised sender %s on account %s",
                message.sender_ref, account.id,
            )
            return

        adapter = self._adapters.get(message.platform.value)
        if adapter is None:
            _LOGGER.warning(
                "no adapter registered for platform %s", message.platform.value,
            )
            return

        binding = await self._find_or_create_binding(account, message.chat_ref)
        if not binding.enabled:
            return

        binding, conversation_id = await self._ensure_conversation(account, binding)
        operator_language = await self._resolve_operator_language(
            account.character_id,
        )
        # Rewrite the parser's canonical zh-TW attachment placeholder into
        # the operator's language before it is stored as the user turn —
        # otherwise a non-Chinese operator sees a Chinese "[使用者傳來…]"
        # line in their history and the LLM reads it as Chinese input.
        message_text = localize_inbound_placeholder_text(
            message.text, operator_language,
        )
        request = SendChatMessageRequest(
            character_id=account.character_id,
            conversation_id=conversation_id,
            message=message_text,
            attachment_urls=list(message.attachment_urls),
            operator_persona_enabled=_persona_safe_for_account(account),
            presence_frame=PresenceFramePayload.from_domain(
                PresenceFrame.messaging(
                    platform=message.platform,
                    has_attachments=bool(message.attachment_urls),
                ),
            ),
        )
        try:
            reply = await self._send_turn(request, binding_id=binding.id)
        except ConversationBusyError:
            # Nothing was written: the lease refuses *before* the turn starts.
            # Give the delivery back so the transport can re-deliver it, and
            # let the caller map that to its own retry semantics.
            await self._rollback_delivery(message, claim)
            _LOGGER.warning(
                "inbound handed back to the transport — conversation busy "
                "binding=%s conversation=%s %s/%s id=%s",
                binding.id,
                conversation_id,
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )
            raise
        except ServerDrainingError:
            # Same shape as busy, different cause: this replica is going away
            # and refused the turn before it wrote anything. Hand the delivery
            # back so the platform re-delivers it to a live replica. Must sit
            # above the generic clause — swallowing a drain would keep the
            # receipt, answer the webhook 200, and lose the message silently.
            await self._rollback_delivery(message, claim)
            _LOGGER.warning(
                "inbound handed back to the transport — replica draining "
                "binding=%s conversation=%s %s/%s id=%s",
                binding.id,
                conversation_id,
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )
            raise
        except Exception:
            # Every other failure keeps the dedup stamps: the turn may have
            # written partial state, and re-running it would double-charge.
            _LOGGER.exception("chat_service failed for binding %s", binding.id)
            return

        if reply.assistant_message is None:
            _LOGGER.info(
                "chat_service queued inbound without immediate outbound "
                "binding=%s conversation=%s",
                binding.id, conversation_id,
            )
            return

        await send_segmented_outbound(
            adapter,
            OutboundMessage(
                platform=message.platform,
                chat_ref=message.chat_ref,
                text=reply.assistant_message.content,
                credentials=account.credentials,
                attachments=await self._build_outbound_attachments(
                    reply.assistant_message.attachments,
                ),
                locale=operator_language,
                # Thread the inbound event's reply affinity through so
                # the adapter can answer on the platform's free reply
                # path (LINE) instead of burning push quota.
                reply_context=message.reply_context,
            ),
        )

    async def _send_turn(
        self, request: SendChatMessageRequest, *, binding_id: str,
    ):  # noqa: ANN202 — ChatReplyResponse, typed at the call site
        """Run the turn, retrying a bounded number of times while busy.

        Raises :class:`ConversationBusyError` once the budget is spent; the
        caller owns the rollback.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._chat.send_message(request)
            except ConversationBusyError:
                if attempt >= self._busy_retry_attempts:
                    raise
                _LOGGER.info(
                    "conversation busy for binding %s — retry %d/%d",
                    binding_id, attempt, self._busy_retry_attempts - 1,
                )
                if self._busy_retry_delay_seconds:
                    await asyncio.sleep(self._busy_retry_delay_seconds)

    async def _claim_delivery(self, message: InboundMessage) -> _ClaimOutcome:
        """Take the durable at-most-once claim on this inbound delivery.

        ``DUPLICATE`` means another instance already owns this delivery (a
        platform retry that landed on a different replica) and the caller must
        drop it, which is exactly the debouncer's drop semantics.

        A ledger failure (DB blip) must not swallow a real user message, so it
        degrades to the pre-existing in-process-only protection rather than
        dropping the turn — reported as ``DEGRADED`` so a later rollback knows
        there is no row of ours to delete.
        """
        if self._receipts is None:
            return _ClaimOutcome.DEGRADED
        try:
            claimed = await self._receipts.try_claim(
                message.platform.value,
                message.account_id,
                message.chat_ref,
                message.platform_message_id,
            )
        except Exception:
            _LOGGER.exception(
                "inbound receipt claim failed %s/%s id=%s — proceeding on "
                "in-process debounce only",
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )
            return _ClaimOutcome.DEGRADED
        if not claimed:
            _LOGGER.info(
                "dropping cross-instance duplicate inbound %s/%s id=%s",
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )
            return _ClaimOutcome.DUPLICATE
        return _ClaimOutcome.OWNED

    async def _rollback_delivery(
        self, message: InboundMessage, claim: _ClaimOutcome,
    ) -> None:
        """Un-stamp a delivery that produced no side effects.

        Both dedup layers have to be rolled back or the re-delivery is eaten:
        the durable receipt blocks other replicas for the retention window, the
        in-process debounce entry blocks this one for its TTL — and the polling
        connector re-delivers within seconds. ``DEGRADED``/``DUPLICATE`` claims
        are never released: we cannot prove the row is ours, and deleting
        another instance's claim would re-open a delivery it is still running.
        """
        if self._debouncer is not None:
            self._debouncer.forget(message)
        if claim is not _ClaimOutcome.OWNED or self._receipts is None:
            return
        try:
            await self._receipts.release_claim(
                message.platform.value,
                message.account_id,
                message.chat_ref,
                message.platform_message_id,
            )
        except Exception:
            # The delivery is now stuck behind its own receipt until retention
            # expires, so this is the diagnostic that explains a lost message.
            _LOGGER.exception(
                "inbound receipt release failed %s/%s id=%s — the platform's "
                "re-delivery of this message will be suppressed",
                message.platform.value,
                message.chat_ref,
                message.platform_message_id,
            )

    async def _resolve_operator_language(self, character_id: str) -> str:
        """Resolve the owning-operator content language for a character.

        Falls back to the ship-first ``zh-TW`` whenever no resolver is
        wired or resolution fails, so external delivery keeps working in
        dev / test setups without an operator-profile backend."""
        if self._operator_language_resolver is None:
            return "zh-TW"
        try:
            language = await self._operator_language_resolver(character_id)
        except Exception:
            _LOGGER.exception(
                "operator language resolve failed character=%s", character_id,
            )
            return "zh-TW"
        return resolve_fallback_language(language)

    async def _build_outbound_attachments(
        self, attachments,  # noqa: ANN001 — DTO list, typed at call site
    ) -> tuple[OutboundAttachment, ...]:
        """Convert chat DTO attachments → ``OutboundAttachment`` tuple.

        Promotes relative ``/v1/public/...`` URLs to absolute using the
        effective messaging public base URL. Platforms that fetch by URL
        and adapters that self-fetch before uploading can't resolve a
        relative path, so without a base URL we drop the attachment and
        log a clear warning rather than sending a broken URL.
        """
        result: list[OutboundAttachment] = []
        public_base_url = await self._resolve_public_base_url()
        for att in attachments:
            url = att.url
            if url.startswith("/"):
                if not public_base_url:
                    _LOGGER.warning(
                        "dropping attachment %s for %s — messaging public "
                        "base URL is not set, external platforms cannot "
                        "fetch a server-relative URL. Set Admin Channel "
                        "settings Public Base URL or APP_BASE_URL to an "
                        "externally reachable URL.",
                        url, att.kind,
                    )
                    continue
                url = f"{public_base_url}{url}"
            result.append(
                OutboundAttachment(
                    kind=att.kind,
                    url=url,
                    mime_type=att.mime_type,
                    caption=att.caption,
                ),
            )
        return tuple(result)

    async def _resolve_public_base_url(self) -> str:
        if self._public_base_url_provider is None:
            return self._public_base_url
        try:
            resolved = await self._public_base_url_provider()
        except Exception:
            _LOGGER.exception(
                "messaging public base URL provider failed; using env fallback",
            )
            return self._public_base_url
        if not isinstance(resolved, str):
            return self._public_base_url
        resolved = resolved.strip().rstrip("/")
        return resolved or self._public_base_url

    async def _find_or_create_binding(
        self, account: MessagingAccount, chat_ref: str,
    ) -> ChannelBinding:
        """Return the binding for this chat, creating it on first contact.

        A binding is an implementation detail of "this chat has started
        talking to this account"; creating it automatically avoids
        asking operators to pre-declare every chat the bot might
        receive messages from.
        """
        existing = await self._bindings.find(account.id, chat_ref)
        if existing is not None:
            return existing
        binding = ChannelBinding.create(
            account_id=account.id, chat_ref=chat_ref, enabled=True,
        )
        await self._bindings.save(binding)
        return binding

    async def _ensure_conversation(
        self, account: MessagingAccount, binding: ChannelBinding,
    ) -> tuple[ChannelBinding, str]:
        if binding.conversation_id is not None:
            existing = await self._conversations.get(binding.conversation_id)
            if existing is not None:
                return binding, existing.id

        conversation = Conversation.start(
            character_id=account.character_id,
            source=account.platform.value,
        )
        await self._conversations.save(conversation)
        updated = binding.with_conversation(conversation.id)
        await self._bindings.save(updated)
        return updated, conversation.id


def _persona_safe_for_account(account: MessagingAccount) -> bool:
    """Allow persona learning only when one external human is identified.

    Kept as a name here because this is the inbound side's vocabulary,
    but the rule itself moved to
    :mod:`kokoro_link.application.services.persona_disclosure_gate` once
    the outbound composers needed the same test — one rule, one place.
    """
    return persona_safe_for_account(account)
