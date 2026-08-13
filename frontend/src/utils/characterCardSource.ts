import type { CharacterCardPreview } from '@/utils/api/characters'

/**
 * Presentation decisions that hinge on *where* a character-card preview's
 * fields came from.
 *
 * A card whose `source` is `'cloud'` is a projection of the Yuralume Cloud
 * official-card catalog: the catalog publishes prose and images, never the
 * structural settings (disposition bands, proactive cadence, world frame,
 * bundled arc counts). Those fields sit at the shared DTO's non-empty
 * defaults on a cloud card — not because the character actually has that
 * disposition or that cadence, but because the response body has to put
 * *something* there. Rendering them as facts about the card would be
 * showing a fabrication, so `CharacterCardFace` gates on {@link isCloudCard}
 * before adding those rows.
 */
export function isCloudCard(
  card: Pick<CharacterCardPreview, 'source'> | null | undefined,
): boolean {
  return card?.source === 'cloud'
}

/**
 * Whether the "translate to my language" toggle has anything left to offer
 * for this card.
 *
 * Hidden for **every** cloud card, not only the ones the catalog answered in
 * the operator's language. An official card is served with a translation a
 * human approved per locale, and its install path never calls a model — so
 * on a cloud card the toggle has nothing to switch. Leaving it visible on
 * the `localized === false` rows made it worse than useless: preview ignored
 * it (the catalog's fallback text came back either way) while install acted
 * on it, so the one control that looked like a preference was really an
 * unannounced charge, and what got installed was not what was on screen.
 *
 * `localized` stays out of this decision deliberately. It is a fact about
 * *which language* the catalog could answer with — worth surfacing as a
 * label — never a fact about whether a translate pass is on offer.
 *
 * A local `.lumecard` is untouched: it has no approved translation anywhere,
 * so the toggle stays exactly where it has always lived for a bundled or
 * uploaded card.
 */
export function shouldHideTranslateToggle(
  card: Pick<CharacterCardPreview, 'source' | 'localized'> | null | undefined,
): boolean {
  return isCloudCard(card)
}

/**
 * Whether this card is an IP-partner character that only Yuralume Cloud
 * serves in full (EC 系列 D1).
 *
 * A cloud-exclusive card is listed like any other — its name, one-line
 * summary and portrait are the shop window, and hiding them would defeat the
 * point of hosting an IP character at all. What changes is everything
 * downstream of "add this one": the catalog publishes no prose for it, there
 * is no card file to download, and a deployment without the credential has
 * no path to either.
 */
export function isCloudExclusiveCard(
  card: Pick<CharacterCardPreview, 'distribution'> | null | undefined,
): boolean {
  return card?.distribution === 'cloud_exclusive'
}

/**
 * Whether the install action should be offered for this card at all.
 *
 * Reads the backend's `installable` verdict rather than recomputing it from
 * `distribution`, and that is the whole point: "may this card be installed
 * here" depends on a service credential the browser cannot see. On a hosted
 * deployment the very same cloud-exclusive card is perfectly installable, so
 * a frontend rule keyed on the licence alone would grey out the button for
 * exactly the players who are paying for it.
 *
 * Absent (an older backend, or an upload preview that never had the field)
 * means installable — the field is a refusal, and silence is not one.
 */
export function canInstallCard(
  card: Pick<CharacterCardPreview, 'installable'> | null | undefined,
): boolean {
  return card?.installable !== false
}

/**
 * Whether to explain an install that is not on offer, and not merely omit it.
 *
 * Only when both are true: the card cannot be installed here *and* the reason
 * is one we can name. A button that is simply absent reads as a bug; "cloud
 * only" reads as a fact about the character, which is what it is.
 */
export function shouldShowCloudOnlyNotice(
  card:
    | Pick<CharacterCardPreview, 'distribution' | 'installable'>
    | null
    | undefined,
): boolean {
  return isCloudExclusiveCard(card) && !canInstallCard(card)
}

/**
 * Whether a *list-level* "translate on install" toggle has anyone left to
 * serve.
 *
 * The admin marketplace has one checkbox for a whole grid rather than one per
 * card, so {@link shouldHideTranslateToggle} — which answers about a single
 * card — cannot be asked directly. The list-level question is whether any row
 * would act on it: a cloud card's install path takes no ``translate``
 * parameter at all, so on an all-official shelf the control is a promise of
 * an LLM translation that nothing downstream can keep. It is not a charge (no
 * model is reached either way), which is precisely why it survived: the only
 * damage is an owner ticking a box and wondering why the card installed in
 * Chinese anyway.
 *
 * A mixed shelf keeps the toggle, because the local packs it ships with do
 * still translate — the cloud rows label themselves instead.
 */
export function shouldOfferInstallTranslateToggle(
  cards: ReadonlyArray<Pick<CharacterCardPreview, 'source'>>,
): boolean {
  return cards.some((card) => !shouldHideTranslateToggle(card))
}

/**
 * Whether the "official cards are unavailable right now" notice belongs on
 * screen.
 *
 * Suppressed during loading and behind a hard `error` — those states already
 * own the message slot, and a Cloud outage note stacked on top of a load
 * spinner or a fetch failure would just be noise. `officialCardsUnavailable`
 * itself never blocks the gallery: local packs and installed characters
 * carry on, so this is a banner, not a replacement for the card stage.
 */
export function shouldShowOfficialCardsUnavailableNotice(state: {
  officialCardsUnavailable: boolean
  loading: boolean
  error: string | null
}): boolean {
  return state.officialCardsUnavailable && !state.loading && !state.error
}
