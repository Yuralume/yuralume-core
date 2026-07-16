// Field categorisation for the BYOK provider settings form.
//
// In the catalog all fields live in a flat ``config_fields`` array per
// provider. To support "fill shared fields once, fill per-capability
// fields per capability" we split them with these maps. Keys appearing
// in ``CAPABILITY_FIELD_KEYS`` render inside the matching capability
// card; everything else renders in the shared block.
//
// ``timeout_seconds`` intentionally lives in every capability set
// because real-world timeouts differ per modality (image ~3 min, LLM
// ~30 s, video ~30 min).
//
// ``base_url`` must stay OUT of every capability set. It is a shared
// connection property for all providers (OpenAI-compatible endpoints,
// local APIs, SearXNG instance root alike). It briefly lived in the
// ``search`` set (2026-07-05 web-search providerization) which — via the
// union-based shared/capability split below — removed the field from the
// UI for every non-search capability, made local/custom providers
// unconfigurable, broke "fetch models", and silently wiped stored
// base_url values on edit-save. Regression report: 2026-07-16.

import type {
  ProviderCatalogEntry,
  ProviderFieldSpec,
} from '@/utils/api/providerSettings'

export const CAPABILITY_FIELD_KEYS: Record<string, ReadonlySet<string>> = {
  llm: new Set([
    'default_model',
    'supports_vision',
    'max_tokens',
    'anthropic_version',
    'timeout_seconds',
  ]),
  embedding: new Set([
    'embedding_model',
    'embedding_dimension',
    'request_dimensions',
    'timeout_seconds',
  ]),
  image: new Set(['image_model', 'default_model', 'timeout_seconds']),
  video: new Set(['default_model', 'timeout_seconds']),
  tts: new Set([
    'tts_model',
    'default_model',
    'voice_id',
    'response_format',
    'timeout_seconds',
  ]),
  // The `search_*` model knobs belong to the OpenAI Responses backend.
  search: new Set([
    'search_depth',
    'search_model',
    'search_context_size',
    'search_tool_type',
    'max_results',
    'timeout_seconds',
  ]),
}

export const ALL_CAPABILITY_FIELD_KEYS: ReadonlySet<string> = new Set(
  Object.values(CAPABILITY_FIELD_KEYS).flatMap(s => Array.from(s)),
)

export function isSharedConfigField(field: ProviderFieldSpec): boolean {
  return !ALL_CAPABILITY_FIELD_KEYS.has(field.key)
}

export function fieldsForCapability(
  entry: ProviderCatalogEntry,
  capability: string,
): ProviderFieldSpec[] {
  const keys = CAPABILITY_FIELD_KEYS[capability] ?? new Set<string>()
  let fields = entry.config_fields.filter(f => keys.has(f.key))
  const hasImageModel = entry.config_fields.some(f => f.key === 'image_model')
  const hasTtsModel = entry.config_fields.some(f => f.key === 'tts_model')
  if (capability === 'image' && hasImageModel) {
    fields = fields.filter(f => f.key !== 'default_model')
  }
  if (capability === 'tts' && hasTtsModel) {
    fields = fields.filter(f => f.key !== 'default_model')
  }
  return fields
}

export function sharedFields(entry: ProviderCatalogEntry): ProviderFieldSpec[] {
  return entry.config_fields.filter(isSharedConfigField)
}

export interface SplitRowConfig {
  shared: Record<string, string | boolean>
  perCapability: Record<string, Record<string, string | boolean>>
}

/**
 * Split a stored connection row's flat config into the shared /
 * per-capability buckets the form renders.
 *
 * A key claimed by a capability set that is NOT among the row's ticked
 * capabilities falls back to the shared bucket instead of being dropped:
 * the shared bucket is round-tripped into the save payload, so a stored
 * value must never silently vanish just because the UI has no card for it.
 */
export function splitRowConfig(
  config: Record<string, unknown>,
  capabilities: readonly string[],
): SplitRowConfig {
  const shared: Record<string, string | boolean> = {}
  const perCapability: Record<string, Record<string, string | boolean>> = {}
  for (const cap of capabilities) perCapability[cap] = {}

  for (const [key, value] of Object.entries(config)) {
    const coerced: string | boolean =
      typeof value === 'boolean' ? value : String(value)
    const owners = capabilities.filter(cap =>
      CAPABILITY_FIELD_KEYS[cap]?.has(key),
    )
    if (owners.length > 0) {
      for (const cap of owners) perCapability[cap][key] = coerced
    } else {
      shared[key] = coerced
    }
  }
  return { shared, perCapability }
}
