/**
 * i18n helpers for backend-sourced "catalog label" families.
 *
 * Two admin surfaces ship single-language labels straight from the
 * backend and the frontend used to render them verbatim, which leaked
 * the source language into non-matching UI locales:
 *
 *   - LLM-routing **feature chips** (`FeatureModelsPicker`) render
 *     ``member.label`` — Chinese strings from
 *     ``feature_keys.FEATURE_LABELS``.
 *   - Provider Keys **dynamic form fields**
 *     (`ProviderSettingsAdminPage`) render ``field.label`` /
 *     ``field.placeholder`` — English strings from the provider
 *     ``catalog``.
 *
 * Both carry a stable ``key`` alongside the human label, so we map the
 * key into a frontend i18n namespace and fall back to the backend
 * string when no translation exists. This is a pure dictionary lookup
 * (key → localized label), not a semantic branch, so it does not
 * violate the LLM-first rule: unknown/new keys degrade gracefully to
 * the backend value with zero risk.
 */

/**
 * Minimal shape of vue-i18n's ``t`` with a default-message fallback.
 * ``t(key, fallback)`` returns the translation when ``key`` resolves,
 * otherwise the ``fallback`` string — exactly the graceful-degradation
 * contract we want for backend-sourced labels.
 */
export type TranslateFn = (key: string, fallback: string) => string

/** Namespace root for per-feature routing chip labels. */
export const FEATURE_KEY_LABEL_NAMESPACE = 'featureModelsPicker.featureKeys'

/** Namespace root for provider dynamic-form field specs. */
export const PROVIDER_FIELD_NAMESPACE = 'admin.providerSettings.providerFields'

export interface FeatureChip {
  key: string
  label: string
}

export interface ProviderFieldLike {
  key: string
  label: string
  placeholder: string
  hint?: string
}

/** Namespace root for provider capability labels (llm / image / …). */
export const CAPABILITY_LABEL_NAMESPACE = 'admin.providerSettings.capabilities'

/** Namespace root for provider display-name descriptors (self-hosted / custom …). */
export const PROVIDER_DISPLAY_NAME_NAMESPACE = 'admin.providerSettings.providerDisplayNames'

const CONNECTION_LABEL_SEPARATOR = ' — '

/**
 * Reverse map of every shipped localized capability label → its stable key.
 *
 * ``ProviderSettingsAdminPage`` composes a connection label as
 * ``<providerName> — <capabilityLabel>`` and PERSISTS it, so the capability
 * suffix freezes into whatever locale the admin used at create time (e.g.
 * "OpenAI — 生圖" then surfaces verbatim in an English UI). This map lets us
 * recognise such an auto-composed suffix regardless of its origin locale and
 * re-localize it at render time. Keep in sync with the
 * ``admin.providerSettings.capabilities`` i18n keys — a unit test guards drift.
 */
const CAPABILITY_LABEL_TO_KEY: Record<string, string> = {
  LLM: 'llm',
  Embedding: 'embedding', 向量: 'embedding',
  Image: 'image', 生圖: 'image', 画像: 'image',
  Video: 'video', 影片: 'video', 動画: 'video',
  TTS: 'tts',
  Search: 'search', 搜尋: 'search', 検索: 'search',
  Cloud: 'cloud',
}

/**
 * Re-localize an auto-composed provider connection label at render time.
 *
 * When the trailing ``— <capability>`` matches a known capability label in any
 * shipped locale, rebuild that suffix from the current locale; a hand-edited
 * custom label (no recognised suffix) passes through untouched. This is the
 * graceful-degradation contract: recognised auto-label → localize, anything
 * else → verbatim. Fixes the frozen "生圖"-style suffix that leaked into
 * non-Chinese UIs (player image-style dropdown, admin cards) without any
 * backend/schema change or migration.
 */
export function providerConnectionLabel(t: TranslateFn, label: string): string {
  const raw = (label || '').trim()
  const idx = raw.lastIndexOf(CONNECTION_LABEL_SEPARATOR)
  if (idx <= 0) return raw
  const name = raw.slice(0, idx)
  const suffix = raw.slice(idx + CONNECTION_LABEL_SEPARATOR.length)
  const capabilityKey = CAPABILITY_LABEL_TO_KEY[suffix]
  if (!capabilityKey) return raw
  const localizedCap = t(`${CAPABILITY_LABEL_NAMESPACE}.${capabilityKey}`, suffix)
  return `${name}${CONNECTION_LABEL_SEPARATOR}${localizedCap}`
}

/**
 * Localized provider display name.
 *
 * Brand names (OpenAI, Anthropic…) have no i18n entry and fall back to the
 * backend ``display_name`` unchanged; only descriptive names carrying an
 * English qualifier ("Custom …", "… (self-hosted)", "(Instant Answer only)")
 * get a ``providerDisplayNames.<id>`` translation. Pure key → label lookup
 * with graceful fallback, so unknown providers degrade to the backend string.
 */
export function providerDisplayNameLabel(
  t: TranslateFn,
  providerId: string,
  fallbackDisplayName: string,
): string {
  const fallback = fallbackDisplayName || providerId
  if (!providerId) return fallback
  return t(`${PROVIDER_DISPLAY_NAME_NAMESPACE}.${providerId}`, fallback)
}

/** Namespace root for image-profile picker feature labels. */
export const IMAGE_FEATURE_KEY_LABEL_NAMESPACE = 'imageProfilesPicker.featureKeys'

/** Namespace root for video-profile picker feature labels. */
export const VIDEO_FEATURE_KEY_LABEL_NAMESPACE = 'videoProfilesPicker.featureKeys'

/**
 * Localized label for a feature-key chip / row.
 *
 * Looks up ``<namespace>.<key>`` and falls back to the backend-provided
 * Chinese ``label`` when the key is not in the catalogue (new/unknown
 * feature key → no localized entry yet). ``namespace`` defaults to the
 * LLM-routing picker; the image / video profile pickers share the same
 * backend ``FEATURE_LABELS`` family but render in their own panels, so
 * they pass their own namespace to keep each ``featureKeys`` block
 * alongside that picker's other copy while reusing this identical
 * lookup + fallback contract.
 */
export function featureKeyLabel(
  t: TranslateFn,
  member: FeatureChip,
  namespace: string = FEATURE_KEY_LABEL_NAMESPACE,
): string {
  const fallback = member.label || member.key
  return t(`${namespace}.${member.key}`, fallback)
}

/**
 * Localized label for a provider dynamic-form field.
 *
 * Field keys (``api_key``, ``base_url``, ``max_tokens``…) are shared
 * across providers, so the namespace is keyed by ``field.key`` alone —
 * one translation serves every provider that reuses the field. Falls
 * back to the backend English ``label`` on a miss.
 */
export function providerFieldLabel(t: TranslateFn, field: ProviderFieldLike): string {
  const fallback = field.label || field.key
  return t(`${PROVIDER_FIELD_NAMESPACE}.${field.key}.label`, fallback)
}

/**
 * Localized placeholder for a provider dynamic-form field. Empty
 * placeholders stay empty (no namespace lookup) so we never surface a
 * translation for a field the backend left blank.
 */
export function providerFieldPlaceholder(t: TranslateFn, field: ProviderFieldLike): string {
  if (!field.placeholder) return ''
  return t(`${PROVIDER_FIELD_NAMESPACE}.${field.key}.placeholder`, field.placeholder)
}

/**
 * Localized persistent hint for a provider dynamic-form field. Unlike a
 * placeholder (which disappears once the user types) this renders as
 * standing helper text under the input. Fields with no backend hint stay
 * hintless (no namespace lookup) so we never surface a translation for a
 * field the catalog left blank.
 */
export function providerFieldHint(t: TranslateFn, field: ProviderFieldLike): string {
  if (!field.hint) return ''
  return t(`${PROVIDER_FIELD_NAMESPACE}.${field.key}.hint`, field.hint)
}
