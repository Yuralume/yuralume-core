import { describe, expect, it } from 'vitest'

import type {
  ProviderCatalogEntry,
  ProviderFieldSpec,
} from '@/utils/api/providerSettings'
import {
  fieldsForCapability,
  isSharedConfigField,
  sharedFields,
  splitRowConfig,
} from '@/utils/providerFields'

function fieldSpec(key: string, overrides: Partial<ProviderFieldSpec> = {}): ProviderFieldSpec {
  return {
    key,
    label: key,
    kind: 'text',
    required: false,
    required_for_capabilities: [],
    placeholder: '',
    secret: false,
    advanced: false,
    ...overrides,
  } as ProviderFieldSpec
}

function catalogEntry(
  id: string,
  capabilities: string[],
  configKeys: string[],
): ProviderCatalogEntry {
  return {
    id,
    display_name: id,
    capabilities,
    auth_fields: [fieldSpec('api_key', { secret: true })],
    config_fields: configKeys.map(k => fieldSpec(k)),
    model_catalog_mode: 'remote',
    default_models: [],
    adapter_kind: 'openai_compatible',
    docs_url: '',
  } as ProviderCatalogEntry
}

describe('providerFields', () => {
  // Regression guard for the 2026-07-16 report: base_url disappeared from
  // the UI for every non-search capability, breaking local/custom provider
  // setup and "fetch models".
  it('renders base_url in the shared block for LLM/image providers', () => {
    const nanogpt = catalogEntry('nanogpt', ['llm', 'image'], [
      'base_url',
      'default_model',
      'image_model',
      'timeout_seconds',
    ])
    const shared = sharedFields(nanogpt).map(f => f.key)
    expect(shared).toContain('base_url')
    expect(fieldsForCapability(nanogpt, 'llm').map(f => f.key)).not.toContain('base_url')
  })

  it('renders the SearXNG instance URL field for a search-only provider', () => {
    // Uses the REAL catalog key (searxng_base_url, re-keyed 2026-07-16 so
    // its i18n hint stops colliding with the generic base_url entry) —
    // this locks the actual shipped shape, not just the principle.
    const searxng = catalogEntry('searxng', ['search'], [
      'searxng_base_url',
      'max_results',
      'timeout_seconds',
    ])
    expect(sharedFields(searxng).map(f => f.key)).toContain('searxng_base_url')
    expect(fieldsForCapability(searxng, 'search').map(f => f.key)).not.toContain('searxng_base_url')
    expect(isSharedConfigField(fieldSpec('base_url'))).toBe(true)
  })

  it('keeps capability-specific model knobs out of the shared block', () => {
    const openrouter = catalogEntry('openrouter', ['llm', 'image'], [
      'base_url',
      'default_model',
      'image_model',
    ])
    const shared = sharedFields(openrouter).map(f => f.key)
    expect(shared).not.toContain('default_model')
    expect(shared).not.toContain('image_model')
    expect(fieldsForCapability(openrouter, 'llm').map(f => f.key)).toContain('default_model')
  })

  it('hides default_model on the image card when image_model exists', () => {
    const entry = catalogEntry('openai', ['image'], [
      'default_model',
      'image_model',
    ])
    const keys = fieldsForCapability(entry, 'image').map(f => f.key)
    expect(keys).toContain('image_model')
    expect(keys).not.toContain('default_model')
  })

  describe('splitRowConfig', () => {
    it('routes stored base_url into the shared bucket', () => {
      const { shared, perCapability } = splitRowConfig(
        { base_url: 'http://127.0.0.1:1234/v1', default_model: 'local-model' },
        ['llm'],
      )
      expect(shared.base_url).toBe('http://127.0.0.1:1234/v1')
      expect(perCapability.llm.default_model).toBe('local-model')
    })

    it('replicates a capability key into every ticked capability that owns it', () => {
      const { perCapability } = splitRowConfig(
        { timeout_seconds: '180' },
        ['llm', 'image'],
      )
      expect(perCapability.llm.timeout_seconds).toBe('180')
      expect(perCapability.image.timeout_seconds).toBe('180')
    })

    it('never drops a stored value whose capability card is not ticked', () => {
      // image_model belongs to the image set, but the row only has llm
      // ticked — the value must survive in the shared bucket so an
      // edit-save round-trip cannot silently wipe it.
      const { shared } = splitRowConfig(
        { image_model: 'flux-1.1-pro' },
        ['llm'],
      )
      expect(shared.image_model).toBe('flux-1.1-pro')
    })

    it('preserves boolean values without stringifying', () => {
      const { perCapability } = splitRowConfig(
        { supports_vision: true },
        ['llm'],
      )
      expect(perCapability.llm.supports_vision).toBe(true)
    })
  })
})
