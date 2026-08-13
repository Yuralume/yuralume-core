/**
 * Pure presentation decisions the character-card gallery makes about a
 * cloud (Yuralume Cloud official-card catalog) vs. local (`.lumecard`)
 * preview — OC6g, OFFICIAL_CARD_CLOUD_CATALOG_PLAN §3.5.
 *
 * `CharacterCardGalleryModal.vue` and `CharacterCardFace.vue` have no DOM
 * mounting infra to test against (this repo runs no jsdom / @vue/test-utils
 * — see `uiImage.test.ts`), so the decisions those components render off
 * of are extracted into `characterCardSource.ts` and pinned here directly.
 */

import { describe, expect, it } from 'vitest'

import { readFileSync } from 'node:fs'

import {
  canInstallCard,
  isCloudCard,
  isCloudExclusiveCard,
  shouldHideTranslateToggle,
  shouldOfferInstallTranslateToggle,
  shouldShowCloudOnlyNotice,
  shouldShowOfficialCardsUnavailableNotice,
} from '@/utils/characterCardSource'
import type { CharacterCardPreview } from '@/utils/api/characters'

// The predicates' contract (`Pick<..., 'source'>`) deliberately leaves
// `localized` out, but every real caller hands over a full preview that
// carries it. A fresh object literal with `localized` trips TS's
// excess-property check against that narrow Pick, so the fixtures that pin
// "localized cannot change the answer" go through this wider alias — a
// variable of an assignable type is exactly what production code passes.
const card = (
  fixture: Pick<CharacterCardPreview, 'source' | 'localized'>,
): Pick<CharacterCardPreview, 'source' | 'localized'> => fixture

describe('isCloudCard', () => {
  it('is true only for a card whose source is the Cloud catalog', () => {
    expect(isCloudCard({ source: 'cloud' })).toBe(true)
    expect(isCloudCard({ source: 'local' })).toBe(false)
  })

  it('treats a missing source as local — the backend DTO default', () => {
    expect(isCloudCard({ source: undefined })).toBe(false)
    expect(isCloudCard(null)).toBe(false)
    expect(isCloudCard(undefined)).toBe(false)
  })
})

describe('shouldHideTranslateToggle', () => {
  it('hides the toggle for a cloud card the catalog already answered in the operator language', () => {
    expect(shouldHideTranslateToggle(card({ source: 'cloud', localized: true }))).toBe(true)
  })

  it('hides it for a cloud card that fell back to another language too', () => {
    // The toggle used to survive here, and it was the worst of both: preview
    // ignored it (the catalog's fallback text came back either way) while
    // install acted on it and paid for an LLM pass. A control that changes
    // nothing on screen but bills on click is not a preference.
    expect(shouldHideTranslateToggle(card({ source: 'cloud', localized: false }))).toBe(true)
  })

  it('hides it for a cloud card with no localized flag at all', () => {
    expect(shouldHideTranslateToggle({ source: 'cloud' })).toBe(true)
  })

  it('never hides the toggle for a local pack, even if localized were somehow true', () => {
    // A local `.lumecard` cannot claim localized=true — only the Cloud
    // catalog resolves a locale chain — but the toggle's visibility must
    // stay pinned to `source`, not to `localized` alone.
    expect(shouldHideTranslateToggle(card({ source: 'local', localized: true }))).toBe(false)
    expect(shouldHideTranslateToggle(card({ source: 'local', localized: false }))).toBe(false)
  })

  it('keeps the toggle when there is no source at all (uploaded-file preview)', () => {
    expect(shouldHideTranslateToggle({})).toBe(false)
    expect(shouldHideTranslateToggle(null)).toBe(false)
  })
})

describe('shouldOfferInstallTranslateToggle', () => {
  it('drops the toggle when every card on the shelf is an official one', () => {
    // The admin marketplace's checkbox is one control for the whole grid, and
    // a cloud card's install takes no `translate` parameter at all — so on an
    // all-official shelf it promised an LLM translation nothing downstream
    // could deliver.
    expect(shouldOfferInstallTranslateToggle([
      { source: 'cloud' }, { source: 'cloud' },
    ])).toBe(false)
  })

  it('keeps it as soon as one local pack is there to use it', () => {
    expect(shouldOfferInstallTranslateToggle([
      { source: 'cloud' }, { source: 'local' },
    ])).toBe(true)
    expect(shouldOfferInstallTranslateToggle([{ source: undefined }])).toBe(true)
  })

  it('drops it on an empty shelf — nothing to install, nothing to offer', () => {
    expect(shouldOfferInstallTranslateToggle([])).toBe(false)
  })
})

describe('CharacterCardMarketplace wiring', () => {
  // The component mounts an ant-design notification API and fetches on
  // `onMounted`, neither of which this repo's SSR-only harness reaches (see
  // `uiImage.test.ts`), so the wiring is pinned by reading the source — the
  // same convention `memoirAvatarImage.test.ts` uses for `MemoirPage`.
  const source = readFileSync(
    new URL('../src/components/admin/CharacterCardMarketplace.vue', import.meta.url),
    'utf8',
  )

  it('gates the translate checkbox on the shared predicate rather than its own comparison', () => {
    expect(source).toContain('shouldOfferInstallTranslateToggle')
    expect(source).toContain('v-if="offersTranslateToggle"')
    // One definition of "this card is from the Cloud catalog". A literal
    // `source === 'cloud'` here is how the player gallery and the console
    // start disagreeing about the same card.
    expect(source).not.toContain("=== 'cloud'")
  })

  it('says why the option does not apply on the official rows', () => {
    expect(source).toContain('isCloudCard(pack)')
    expect(source).toContain('marketplace.officialTranslated')
  })
})

describe('shouldShowOfficialCardsUnavailableNotice', () => {
  it('shows the notice once the catalog fetch settled unavailable', () => {
    expect(shouldShowOfficialCardsUnavailableNotice({
      officialCardsUnavailable: true, loading: false, error: null,
    })).toBe(true)
  })

  it('stays quiet while the shelf loads — no bad news yet', () => {
    expect(shouldShowOfficialCardsUnavailableNotice({
      officialCardsUnavailable: true, loading: true, error: null,
    })).toBe(false)
  })

  it('yields to a hard fetch error — that message slot already covers it', () => {
    expect(shouldShowOfficialCardsUnavailableNotice({
      officialCardsUnavailable: true, loading: false, error: 'network down',
    })).toBe(false)
  })

  it('stays quiet when the catalog is simply not unavailable', () => {
    expect(shouldShowOfficialCardsUnavailableNotice({
      officialCardsUnavailable: false, loading: false, error: null,
    })).toBe(false)
  })
})

describe('cloud-exclusive cards (EC4)', () => {
  it('recognises the IP-partner distribution and nothing else', () => {
    expect(isCloudExclusiveCard({ distribution: 'cloud_exclusive' })).toBe(true)
    expect(isCloudExclusiveCard({ distribution: 'public' })).toBe(false)
    // A local pack / upload preview never carries the field.
    expect(isCloudExclusiveCard({ distribution: undefined })).toBe(false)
    expect(isCloudExclusiveCard(null)).toBe(false)
  })

  it('offers install unless the backend explicitly refused', () => {
    // `installable` is a refusal, so silence is not one: an older backend
    // and an upload preview must both keep their button.
    expect(canInstallCard({ installable: undefined })).toBe(true)
    expect(canInstallCard(null)).toBe(true)
    expect(canInstallCard({ installable: true })).toBe(true)
    expect(canInstallCard({ installable: false })).toBe(false)
  })

  it('keeps the button on a cloud-exclusive card the deployment CAN install', () => {
    // The same card is installable on hosted and not on self-host. A rule
    // keyed on the licence alone would grey it out for exactly the players
    // who are paying for it — the credential is what decides, and only the
    // backend can see it.
    const installableExclusive = {
      distribution: 'cloud_exclusive' as const, installable: true,
    }
    expect(canInstallCard(installableExclusive)).toBe(true)
    expect(shouldShowCloudOnlyNotice({
      distribution: 'cloud_exclusive', installable: true,
    })).toBe(false)
  })

  it('explains a refused install only when it can name the reason', () => {
    expect(shouldShowCloudOnlyNotice({
      distribution: 'cloud_exclusive', installable: false,
    })).toBe(true)
    // Refused for some other reason (a future gate): no "cloud only" claim.
    expect(shouldShowCloudOnlyNotice({
      distribution: 'public', installable: false,
    })).toBe(false)
  })
})

describe('CharacterCardFace wiring', () => {
  const source = readFileSync(
    new URL('../src/components/CharacterCardFace.vue', import.meta.url),
    'utf8',
  )

  it('disables the action and shows the chip off the shared predicates', () => {
    expect(source).toContain('canInstallCard')
    expect(source).toContain('shouldShowCloudOnlyNotice')
    expect(source).toContain(':disabled="actionDisabled || installBlocked"')
    expect(source).toContain('playerSidebar.characterCards.cloudOnly.chip')
    expect(source).toContain('playerSidebar.characterCards.cloudOnly.hint')
    // One definition of "this deployment cannot install this". A literal
    // comparison here is how the card face and the console start
    // disagreeing about the same card.
    expect(source).not.toContain("=== 'cloud_exclusive'")
  })
})

describe('PlayerCharacterCardPanel wiring', () => {
  const source = readFileSync(
    new URL('../src/components/PlayerCharacterCardPanel.vue', import.meta.url),
    'utf8',
  )

  it('refuses the install path itself, not only the button', () => {
    // The disabled button is the explanation; this is the stale-shelf /
    // keyboard path, and the backend refuses behind both.
    expect(source).toContain('if (!canInstallCard(card)) return')
  })
})
