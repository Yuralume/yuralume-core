"""Inbound messaging dispatcher.

Inbound flow (platform-agnostic once the adapter has normalised payload):

1. Debounce by ``platform_message_id`` — webhook retries don't double-fire
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
"""

import logging
from collections.abc import Awaitable, Callable

from kokoro_link.application.dto.chat import PresenceFramePayload, SendChatMessageRequest
from kokoro_link.application.services.chat_service import ChatService
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
            reply = await self._chat.send_message(request)
        except Exception:
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
            ),
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

    Empty allowlist means "accept anyone" and multi-entry allowlists can
    represent group / shared accounts. Both cases would write several
    humans into the same DEFAULT_OPERATOR_ID, so persona extraction is
    disabled for those inbound turns.
    """
    senders = tuple(ref for ref in account.allowed_sender_refs if ref)
    return len(senders) == 1
