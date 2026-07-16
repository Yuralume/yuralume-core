<script setup lang="ts">
import { computed, h, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { notification } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton, UiCard, UiCombobox } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  formatLatency,
  formatProbeLines,
  orderProbes,
  probeStatusMark,
} from '@/utils/probeReportDisplay'
import {
  providerConnectionLabel,
  providerDisplayNameLabel,
  providerFieldHint,
  providerFieldLabel,
  providerFieldPlaceholder,
} from '@/utils/catalogLabels'
import {
  createProviderConnection,
  deleteProviderConnection,
  listComfyuiCheckpoints,
  listProviderCatalog,
  listProviderConnections,
  listProviderModels,
  testDraftProviderConnection,
  testProviderConnection,
  updateProviderConnection,
  type ProbeReport,
  type ProviderCatalogEntry,
  type ProviderConnection,
  type ProviderConnectionPayload,
  type ProviderFieldSpec,
} from '@/utils/api/providerSettings'
import {
  fieldsForCapability,
  sharedFields,
  splitRowConfig,
} from '@/utils/providerFields'

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

// ---------------------------------------------------------------------------
// Field categorisation — shared/per-capability split lives in
// utils/providerFields.ts (unit-tested; see providerFields.test.ts for the
// base_url regression guard).
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Reactive state
// ---------------------------------------------------------------------------

interface CapabilityFormState {
  label: string
  config: Record<string, string | boolean>
}

const catalog = ref<ProviderCatalogEntry[]>([])
const connections = ref<ProviderConnection[]>([])
const loading = ref(false)
const saving = ref(false)
const testingId = ref<string | null>(null)
const testingCapability = ref<string | null>(null)
const deepTestingCapability = ref<string | null>(null)
const editingId = ref<string | null>(null)

// Per-capability live-probe results from the draft test-draft route.
// Keyed by capability; cleared whenever the draft provider changes so a
// stale card never shows probes from a previous provider.
const capabilityProbes = reactive<Record<string, ProbeReport[]>>({})

function clearCapabilityProbes(): void {
  for (const key of Object.keys(capabilityProbes)) {
    delete capabilityProbes[key]
  }
}

// Per-(capability, field-key) cached model lists from
// `/admin/providers/list-models`. The keys we ever fetch are bounded by
// the current draft, so we use a `Record` rather than a Map — keeps the
// template ergonomics straightforward.
const modelOptions = reactive<Record<string, string[]>>({})
const modelFetching = reactive<Record<string, boolean>>({})

// ComfyUI checkpoint dropdown state. Keyed by capability+field like the
// model options, but populated from GET /system/comfyui/checkpoints. When
// the fetch fails the field degrades to plain-text entry (the datalist is
// simply empty), never blocking the provider form (Phase 2 risk note).
const checkpointOptions = reactive<Record<string, string[]>>({})
const checkpointFetching = reactive<Record<string, boolean>>({})

function isCheckpointField(field: ProviderFieldSpec): boolean {
  return field.kind === 'comfyui_checkpoint'
}

async function fetchCheckpointsFor(
  capability: string,
  fieldKey: string,
): Promise<void> {
  const key = modelOptionsKey(capability, fieldKey)
  const server = String(
    form.perCapability[capability]?.config?.server ?? '',
  ).trim()
  if (!server) {
    notification.info({
      message: t('admin.providerSettings.comfyuiCheckpointNeedsServer'),
      duration: 3,
    })
    return
  }
  checkpointFetching[key] = true
  try {
    const result = await listComfyuiCheckpoints(server)
    if (!result.available) {
      notification.warning({
        message: t('admin.providerSettings.comfyuiCheckpointFetchFailed'),
        description: result.error,
        duration: 5,
      })
      checkpointOptions[key] = []
      return
    }
    checkpointOptions[key] = result.checkpoints ?? []
  } catch (err) {
    notification.warning({
      message: t('admin.providerSettings.comfyuiCheckpointFetchFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 5,
    })
    checkpointOptions[key] = []
  } finally {
    checkpointFetching[key] = false
  }
}

function modelOptionsKey(capability: string, fieldKey: string): string {
  return `${capability}::${fieldKey}`
}

function isFieldRequired(field: ProviderFieldSpec, capability: string): boolean {
  if (field.required) return true
  return field.required_for_capabilities?.includes(capability) ?? false
}

function supportsModelDiscovery(
  entry: ProviderCatalogEntry | undefined,
  capability: string,
): boolean {
  if (!entry) return false
  if (entry.id === 'yuralume_cloud') {
    return capability === 'llm' || capability === 'image' || capability === 'video'
  }
  if (entry.adapter_kind === 'openai' || entry.adapter_kind === 'openai_compatible') {
    return capability === 'llm' || capability === 'embedding' || capability === 'image' || capability === 'tts' || capability === 'search'
  }
  return false
}

function isModelField(field: ProviderFieldSpec): boolean {
  return (
    field.key === 'default_model'
    || field.key === 'embedding_model'
    || field.key === 'image_model'
    || field.key === 'tts_model'
    || field.key === 'search_model'
  )
}

const form = reactive({
  provider: 'openai',
  enabled: true,
  capabilities: [] as string[],
  shared: {} as Record<string, string | boolean>,
  perCapability: {} as Record<string, CapabilityFormState>,
  secret: {} as Record<string, string>,
  clear_secret: false,
})

const selectedCatalog = computed(() =>
  catalog.value.find(entry => entry.id === form.provider) ?? catalog.value[0],
)

const editingConnection = computed(() =>
  editingId.value
    ? connections.value.find(row => row.id === editingId.value) ?? null
    : null,
)

const visibleSharedFields = computed(() =>
  selectedCatalog.value ? sharedFields(selectedCatalog.value) : [],
)

const orderedCapabilities = computed(() => {
  const entry = selectedCatalog.value
  if (!entry) return []
  return entry.capabilities.filter(cap => form.capabilities.includes(cap))
})

const capabilityTabs = ['llm', 'embedding', 'image', 'video', 'tts', 'search', 'cloud']
const activeCapability = ref('llm')

const visibleConnections = computed(() => {
  if (activeCapability.value === 'cloud') {
    return connections.value.filter(row => row.provider === 'yuralume_cloud')
  }
  return connections.value.filter(row =>
    row.capabilities.includes(activeCapability.value),
  )
})

// Capabilities where runtime_sync mounts exactly ONE backend even when
// several rows are enabled (`_sync_search_tool` / `_sync_tts_backend` /
// `_sync_embedding_backend` all pick `max(rows, key=updated_at)`). For
// these tabs the "enabled" badge alone is misleading — two green rows,
// only one actually serving. llm/image/video register every enabled row,
// so they keep the plain enabled/disabled badge.
const SINGLE_ACTIVE_CAPABILITIES: ReadonlySet<string> = new Set([
  'search',
  'embedding',
  'tts',
])

const isSingleActiveTab = computed(() =>
  SINGLE_ACTIVE_CAPABILITIES.has(activeCapability.value),
)

// Mirror the backend's single-active selection for display only: among
// enabled rows on this tab, the most-recently-updated one (updated_at,
// else created_at) is the row runtime_sync would mount. The DB remains
// the source of truth — this is a pure read, never persisted.
const activeConnectionId = computed<string | null>(() => {
  if (!isSingleActiveTab.value) return null
  let winnerId: string | null = null
  let winnerTs = Number.NEGATIVE_INFINITY
  for (const row of visibleConnections.value) {
    if (!row.enabled) continue
    const parsed = Date.parse(row.updated_at ?? row.created_at ?? '')
    const ts = Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed
    if (winnerId === null || ts > winnerTs) {
      winnerId = row.id
      winnerTs = ts
    }
  }
  return winnerId
})

type ConnectionStatus = 'active' | 'standby' | 'enabled' | 'disabled'

function connectionStatus(row: ProviderConnection): ConnectionStatus {
  if (!row.enabled) return 'disabled'
  if (!isSingleActiveTab.value) return 'enabled'
  return row.id === activeConnectionId.value ? 'active' : 'standby'
}

function connectionBadge(
  row: ProviderConnection,
): { variant: 'default' | 'success'; label: string } {
  switch (connectionStatus(row)) {
    case 'active':
      return { variant: 'success', label: t('admin.providerSettings.activeBadge') }
    case 'standby':
      return { variant: 'default', label: t('admin.providerSettings.standbyBadge') }
    case 'disabled':
      return { variant: 'default', label: t('admin.providerSettings.disabled') }
    default:
      return { variant: 'success', label: t('admin.providerSettings.enabled') }
  }
}

// `listProviderConnections()` only ever returns rows the user actually
// created (unlike the runtime `/system/providers` registry, which can
// report the `fake` placeholder backend when nothing is configured).
// So a plain length check is enough to know "at least one real
// provider exists" for the routing next-step card below.
const hasAnyProvider = computed(() => connections.value.length > 0)

// When the provider dropdown changes (in create mode), reset the form
// to the new provider's defaults: auto-tick the first capability so
// the user immediately sees one config card.
watch(
  () => form.provider,
  () => {
    if (editingId.value) return
    resetFormForCurrentProvider()
  },
)

function resetFormForCurrentProvider(): void {
  const entry = selectedCatalog.value
  clearCapabilityProbes()
  form.shared = {}
  form.perCapability = {}
  form.secret = {}
  form.clear_secret = false
  form.enabled = true
  if (entry?.capabilities.length) {
    form.capabilities = [entry.capabilities[0]]
    ensureCapabilityState(entry.capabilities[0])
  } else {
    form.capabilities = []
  }
}

function defaultLabelFor(providerId: string, capability: string): string {
  const entry = catalog.value.find(row => row.id === providerId)
  const providerName = entry?.display_name ?? providerId
  const capLabel = t(`admin.providerSettings.capabilities.${capability}`)
  return `${providerName} — ${capLabel}`
}

function ensureCapabilityState(capability: string): void {
  if (form.perCapability[capability]) return
  form.perCapability[capability] = {
    label: defaultLabelFor(form.provider, capability),
    config: {},
  }
}

function toggleCapability(capability: string): void {
  if (editingId.value) return
  const idx = form.capabilities.indexOf(capability)
  if (idx >= 0) {
    form.capabilities.splice(idx, 1)
  } else {
    form.capabilities.push(capability)
    ensureCapabilityState(capability)
  }
}

// ---------------------------------------------------------------------------
// Data loading + create/edit lifecycle
// ---------------------------------------------------------------------------

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [catalogRows, connectionRows] = await Promise.all([
      listProviderCatalog(),
      listProviderConnections(),
    ])
    catalog.value = catalogRows
    connections.value = connectionRows
    if (!catalogRows.some(row => row.id === form.provider) && catalogRows[0]) {
      form.provider = catalogRows[0].id
    } else {
      resetFormForCurrentProvider()
    }
  } catch (err) {
    notification.error({
      message: t('admin.providerSettings.errors.loadFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 4,
    })
  } finally {
    loading.value = false
  }
}

function startCreate(providerId?: string): void {
  editingId.value = null
  if (providerId && catalog.value.some(row => row.id === providerId)) {
    form.provider = providerId
  }
  resetFormForCurrentProvider()
}

function startEdit(row: ProviderConnection): void {
  clearCapabilityProbes()
  editingId.value = row.id
  form.provider = row.provider
  form.enabled = row.enabled
  form.capabilities = [...row.capabilities]
  form.secret = {}
  form.clear_secret = false

  // Split row.config into shared vs per-capability buckets so the user
  // sees the same layout as create mode. splitRowConfig never drops a
  // stored value — unclaimed keys fall back to the shared bucket so an
  // edit-save round-trip cannot silently wipe config.
  const { shared, perCapability: perCap } = splitRowConfig(
    row.config,
    row.capabilities,
  )
  form.shared = shared
  form.perCapability = {}
  for (const cap of row.capabilities) {
    form.perCapability[cap] = {
      label: row.label,
      config: perCap[cap] ?? {},
    }
  }
}

// ---------------------------------------------------------------------------
// Save: create-bulk or update-single
// ---------------------------------------------------------------------------

function payloadFor(capability: string): ProviderConnectionPayload {
  const capState = form.perCapability[capability]
  return {
    provider: form.provider,
    label: capState?.label?.trim() || defaultLabelFor(form.provider, capability),
    enabled: form.enabled,
    capabilities: [capability],
    config: { ...form.shared, ...(capState?.config ?? {}) },
    secret: form.secret,
  }
}

async function save(): Promise<void> {
  if (form.capabilities.length === 0) return
  saving.value = true
  try {
    if (editingId.value) {
      // A single DB row can hold multiple capabilities (legacy seeds or
      // user-merged rows). Merge every per-capability card's config back
      // into a single payload so we don't accidentally drop fields when
      // updating a multi-cap row.
      const mergedConfig: Record<string, string | boolean> = { ...form.shared }
      for (const cap of form.capabilities) {
        Object.assign(mergedConfig, form.perCapability[cap]?.config ?? {})
      }
      const primaryCap = form.capabilities[0]
      const primaryLabel =
        form.perCapability[primaryCap]?.label?.trim()
        || defaultLabelFor(form.provider, primaryCap)
      await updateProviderConnection(editingId.value, {
        provider: form.provider,
        label: primaryLabel,
        enabled: form.enabled,
        capabilities: form.capabilities,
        config: mergedConfig,
        secret: form.secret,
        clear_secret: form.clear_secret,
      })
      notification.success({
        message: t('admin.providerSettings.saved'),
        duration: 2,
      })
      await loadAll()
      startCreate(form.provider)
      return
    }

    const targets = [...form.capabilities]
    const results = await Promise.allSettled(
      targets.map(cap => createProviderConnection(payloadFor(cap))),
    )
    const failures = results
      .map((r, i) => ({ status: r.status, cap: targets[i], reason: r.status === 'rejected' ? (r as PromiseRejectedResult).reason : null }))
      .filter(item => item.status === 'rejected')

    if (failures.length === 0) {
      notification.success({
        message: t('admin.providerSettings.saved'),
        duration: 2,
      })
    } else if (failures.length === targets.length) {
      notification.error({
        message: t('admin.providerSettings.allFailed'),
        description: humanizeError(failures[0].reason),
        duration: 5,
      })
    } else {
      notification.warning({
        message: t('admin.providerSettings.partialFailure', {
          success: targets.length - failures.length,
          failed: failures.length,
          caps: failures.map(f => capabilityLabel(f.cap)).join(t('common.listSeparator')),
        }),
        description: humanizeError(failures[0].reason),
        duration: 6,
      })
    }
    await loadAll()
    startCreate(form.provider)
  } catch (err) {
    notification.error({
      message: t('admin.providerSettings.errors.saveFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

async function fetchModelsFor(capability: string, fieldKey: string): Promise<void> {
  const key = modelOptionsKey(capability, fieldKey)
  modelFetching[key] = true
  try {
    const draft = payloadFor(capability)
    const result = await listProviderModels({
      provider: form.provider,
      capability,
      config: draft.config,
      secret: form.secret,
      connection_id: editingId.value,
    })
    if (result.error) {
      notification.warning({
        message: t('admin.providerSettings.modelFetchFailed', { capability: capabilityLabel(capability) }),
        description: result.error,
        duration: 5,
      })
    }
    modelOptions[key] = result.models ?? []
    if (result.models?.length === 0 && !result.error) {
      notification.info({
        message: t('admin.providerSettings.modelFetchEmpty'),
        duration: 3,
      })
    }
  } catch (err) {
    notification.error({
      message: t('admin.providerSettings.modelFetchFailed', { capability: capabilityLabel(capability) }),
      description: err instanceof Error ? err.message : String(err),
      duration: 5,
    })
  } finally {
    modelFetching[key] = false
  }
}

// Localized short label for a probe action token; falls back to the raw
// token if the backend ever sends one outside the contract enum.
function probeActionLabel(action: string): string {
  const key = `admin.providerSettings.probeActions.${action}`
  const label = t(key)
  return label === key ? action : label
}

// Shared runner for the two capability-card test buttons (shallow + deep).
// Stores the per-capability probe list for the inline results panel and
// keeps the existing pass/fail notification.
async function runCapabilityProbe(capability: string, deep: boolean): Promise<void> {
  const busyRef = deep ? deepTestingCapability : testingCapability
  busyRef.value = capability
  try {
    const result = await testDraftProviderConnection(payloadFor(capability), deep)
    capabilityProbes[capability] = result.probes ?? []
    if (result.ok) {
      notification.success({
        message: t('admin.providerSettings.testPassed'),
        duration: 3,
      })
    } else {
      notification.warning({
        message: t('admin.providerSettings.testSavedWithIssue'),
        description: result.last_validation_error ?? '',
        duration: 5,
      })
    }
  } catch (err) {
    notification.error({
      message: t('admin.providerSettings.errors.testFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 4,
    })
  } finally {
    busyRef.value = null
  }
}

function testCapabilityCard(capability: string): Promise<void> {
  return runCapabilityProbe(capability, false)
}

// Deep test really generates one small image through the provider, so it
// gates behind a confirm dialog explaining the (tiny) cost before probing
// with deep=true.
async function deepTestCapabilityCard(capability: string): Promise<void> {
  const confirmed = await confirmDialog({
    title: t('admin.providerSettings.deepTest.confirmTitle'),
    content: t('admin.providerSettings.deepTest.confirmBody'),
    okText: t('admin.providerSettings.deepTest.button'),
  })
  if (!confirmed) return
  await runCapabilityProbe(capability, true)
}

async function remove(row: ProviderConnection): Promise<void> {
  if (!await confirmDialog({
    content: t('admin.providerSettings.confirmDelete', { label: connectionLabel(row.label) }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  await deleteProviderConnection(row.id)
  await loadAll()
  if (editingId.value === row.id) startCreate()
}

async function test(row: ProviderConnection): Promise<void> {
  testingId.value = row.id
  try {
    const updated = await testProviderConnection(row.id)
    connections.value = connections.value.map(item =>
      item.id === updated.id ? updated : item,
    )
    // Description = one line per probe (multi-line, ordered by capability);
    // fall back to the stored validation error when no probes came back.
    const probes = updated.probes ?? []
    const description = probes.length
      ? h(
          'div',
          { style: 'white-space: pre-line' },
          formatProbeLines(probes, probeActionLabel),
        )
      : (updated.last_validation_error ?? undefined)
    notification.success({
      message: updated.last_validation_error
        ? t('admin.providerSettings.testSavedWithIssue')
        : t('admin.providerSettings.testPassed'),
      description,
      duration: probes.length ? 6 : 3,
    })
  } catch (err) {
    notification.error({
      message: t('admin.providerSettings.errors.testFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 4,
    })
  } finally {
    testingId.value = null
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function providerLabel(providerId: string): string {
  const entry = catalog.value.find(row => row.id === providerId)
  return providerDisplayNameLabel(t, providerId, entry?.display_name ?? providerId)
}

/**
 * Re-localize a stored connection label for display. Existing rows froze the
 * capability suffix in the creator's locale (e.g. "OpenAI — 生圖"); this
 * rebuilds that suffix in the current UI language while leaving custom labels
 * untouched. Editing still loads the raw stored label (see loadForEdit).
 */
function connectionLabel(label: string): string {
  return providerConnectionLabel(t, label)
}

function capabilityLabel(capability: string): string {
  return t(`admin.providerSettings.capabilities.${capability}`)
}

function fieldInputType(kind: string): string {
  if (kind === 'number') return 'number'
  return 'text'
}

// Field label/placeholder come from the backend provider catalog in
// English; route them through the shared `providerFields` i18n
// namespace (keyed by field.key so shared fields aren't duplicated per
// provider) and fall back to the backend string on a miss.
function fieldLabel(field: ProviderFieldSpec): string {
  return providerFieldLabel(t, field)
}

function fieldPlaceholder(field: ProviderFieldSpec): string {
  return providerFieldPlaceholder(t, field)
}

// Persistent helper text under an input (routed through the same
// providerFields i18n namespace, keyed `<field.key>.hint`). Empty when
// the catalog spec ships no hint.
function fieldHint(field: ProviderFieldSpec): string {
  return providerFieldHint(t, field)
}

function humanizeError(reason: unknown): string {
  if (reason instanceof Error) return reason.message
  if (typeof reason === 'string') return reason
  try {
    return JSON.stringify(reason)
  } catch {
    return String(reason)
  }
}

const submitLabel = computed(() => {
  if (editingId.value) {
    return t('admin.providerSettings.actions.update')
  }
  return t('admin.providerSettings.actions.createAll', {
    count: form.capabilities.length,
  })
})

const bulkSummary = computed(() => {
  if (editingId.value) return ''
  if (form.capabilities.length === 0) return ''
  return t('admin.providerSettings.bulkSummary', {
    count: form.capabilities.length,
    caps: form.capabilities.map(capabilityLabel).join(t('common.listSeparator')),
  })
})

onMounted(loadAll)
</script>

<template>
  <div class="provider-settings">
    <header class="provider-settings__header">
      <div>
        <h1>{{ t('admin.providerSettings.title') }}</h1>
        <p class="provider-settings__subtitle">{{ t('admin.providerSettings.subtitle') }}</p>
      </div>
      <UiBadge variant="success">{{ t('admin.providerSettings.byokBadge') }}</UiBadge>
    </header>

    <div class="provider-settings__grid">
      <!-- Left: stepper-style create / edit form ------------------------- -->
      <div class="provider-settings__form-column">
        <UiCard size="lg">
          <template #header>
            <div class="provider-settings__form-header">
              <h2 class="provider-settings__card-title">
                {{ editingConnection ? t('admin.providerSettings.editTitle') : t('admin.providerSettings.createTitle') }}
              </h2>
              <UiButton
                v-if="editingId"
                size="sm"
                variant="ghost"
                @click="startCreate(form.provider)"
              >
                {{ t('admin.providerSettings.actions.cancel') }}
              </UiButton>
            </div>
            <p v-if="!editingId" class="provider-settings__hint">
              {{ t('admin.providerSettings.bulkCreateHint') }}
            </p>
            <p v-else class="provider-settings__hint">
              {{ t('admin.providerSettings.editingLockHint') }}
            </p>
          </template>

          <div v-if="loading" class="provider-settings__hint">
            {{ t('admin.providerSettings.loading') }}
          </div>

          <form v-else class="provider-settings__form" @submit.prevent="save">
            <!-- Step 1: provider picker -->
            <section class="provider-settings__step">
              <span class="provider-settings__step-index">1</span>
              <div class="provider-settings__step-body">
                <label class="field-label">
                  {{ t('admin.providerSettings.fields.provider') }}
                  <select
                    v-model="form.provider"
                    class="field-select"
                    :disabled="Boolean(editingId)"
                  >
                    <option v-for="entry in catalog" :key="entry.id" :value="entry.id">
                      {{ providerLabel(entry.id) }}
                    </option>
                  </select>
                </label>

                <RouterLink
                  v-if="form.provider === 'custom_media_gateway'"
                  to="/admin/dev-docs/custom-media-gateway"
                  class="provider-settings__view-spec-link"
                >
                  <UiButton size="sm" variant="ghost" type="button">
                    {{ t('admin.providerSettings.viewSpec') }}
                  </UiButton>
                </RouterLink>

                <RouterLink
                  v-if="form.provider === 'custom_tts'"
                  to="/admin/dev-docs/custom-tts-server"
                  class="provider-settings__view-spec-link"
                >
                  <UiButton size="sm" variant="ghost" type="button">
                    {{ t('admin.providerSettings.viewSpec') }}
                  </UiButton>
                </RouterLink>

                <label class="provider-settings__checkbox">
                  <input v-model="form.enabled" type="checkbox" />
                  <span>{{ t('admin.providerSettings.fields.enabled') }}</span>
                </label>
              </div>
            </section>

            <!-- Step 2: capability picker -->
            <section class="provider-settings__step">
              <span class="provider-settings__step-index">2</span>
              <div class="provider-settings__step-body">
                <h3 class="provider-settings__step-title">
                  {{ t('admin.providerSettings.capabilitiesPickerTitle') }}
                </h3>
                <p class="provider-settings__hint">
                  {{ t('admin.providerSettings.capabilitiesPickerHint') }}
                </p>
                <div v-if="selectedCatalog" class="provider-settings__capabilities">
                  <button
                    v-for="capability in selectedCatalog.capabilities"
                    :key="capability"
                    type="button"
                    class="provider-settings__capability"
                    :class="{
                      'is-active': form.capabilities.includes(capability),
                      'is-locked': editingId && !form.capabilities.includes(capability),
                    }"
                    :disabled="Boolean(editingId) && !form.capabilities.includes(capability)"
                    @click="toggleCapability(capability)"
                  >
                    {{ capabilityLabel(capability) }}
                  </button>
                </div>
              </div>
            </section>

            <!-- Step 3: shared config -->
            <section
              v-if="visibleSharedFields.length || selectedCatalog?.auth_fields.length"
              class="provider-settings__step"
            >
              <span class="provider-settings__step-index">3</span>
              <div class="provider-settings__step-body">
                <h3 class="provider-settings__step-title">
                  {{ t('admin.providerSettings.sharedConfigTitle') }}
                </h3>
                <p class="provider-settings__hint">
                  {{ t('admin.providerSettings.sharedConfigHint') }}
                </p>

                <!-- Auth (secret) goes in shared because one key serves all caps -->
                <template v-if="selectedCatalog?.auth_fields.length">
                  <p
                    v-if="editingConnection?.secret.configured"
                    class="provider-settings__hint provider-settings__hint--inline"
                  >
                    {{ t('admin.providerSettings.secretConfigured', { fingerprint: editingConnection.secret.fingerprint }) }}
                  </p>
                  <label
                    v-for="field in selectedCatalog.auth_fields"
                    :key="`secret-${field.key}`"
                    class="field-label"
                  >
                    {{ fieldLabel(field) }}
                    <input
                      v-model="form.secret[field.key]"
                      class="field-input"
                      :type="field.kind === 'password' ? 'password' : 'text'"
                      :placeholder="fieldPlaceholder(field)"
                      :required="field.required && !editingConnection?.secret.configured"
                      autocomplete="off"
                    />
                  </label>
                  <label
                    v-if="editingConnection?.secret.configured"
                    class="provider-settings__checkbox"
                  >
                    <input v-model="form.clear_secret" type="checkbox" />
                    <span>{{ t('admin.providerSettings.fields.clearSecret') }}</span>
                  </label>
                </template>

                <div
                  v-for="field in visibleSharedFields"
                  :key="`shared-${field.key}`"
                  class="provider-settings__field"
                >
                  <label class="field-label">
                    {{ fieldLabel(field) }}
                    <input
                      v-if="field.kind === 'checkbox'"
                      v-model="form.shared[field.key]"
                      type="checkbox"
                    />
                    <input
                      v-else
                      v-model="form.shared[field.key]"
                      class="field-input"
                      :type="fieldInputType(field.kind)"
                      :placeholder="fieldPlaceholder(field)"
                      :required="field.required"
                    />
                  </label>
                  <p v-if="fieldHint(field)" class="field-hint">{{ fieldHint(field) }}</p>
                </div>
              </div>
            </section>

            <!-- Step 4: one card per ticked capability -->
            <section class="provider-settings__step">
              <span class="provider-settings__step-index">4</span>
              <div class="provider-settings__step-body">
                <p
                  v-if="orderedCapabilities.length === 0"
                  class="provider-settings__hint provider-settings__hint--empty"
                >
                  {{ t('admin.providerSettings.noCapabilitySelected') }}
                </p>

                <div class="provider-settings__cap-cards">
                  <UiCard
                    v-for="capability in orderedCapabilities"
                    :key="`cap-card-${capability}`"
                    class="provider-settings__cap-card"
                  >
                    <template #header>
                      <div class="provider-settings__cap-card-header">
                        <h4 class="provider-settings__cap-card-title">
                          {{ t('admin.providerSettings.capabilityCardTitle', { capability: capabilityLabel(capability) }) }}
                        </h4>
                        <div class="provider-settings__cap-card-actions">
                          <UiButton
                            size="sm"
                            variant="ghost"
                            :loading="testingCapability === capability"
                            type="button"
                            @click="testCapabilityCard(capability)"
                          >
                            {{ t('admin.providerSettings.actions.testDraft') }}
                          </UiButton>
                          <UiButton
                            v-if="capability === 'image'"
                            size="sm"
                            variant="ghost"
                            :loading="deepTestingCapability === capability"
                            type="button"
                            @click="deepTestCapabilityCard(capability)"
                          >
                            {{ t('admin.providerSettings.deepTest.button') }}
                          </UiButton>
                        </div>
                      </div>
                    </template>

                    <label class="field-label">
                      {{ t('admin.providerSettings.fields.perConnectionLabel') }}
                      <input
                        v-model="form.perCapability[capability].label"
                        class="field-input"
                        type="text"
                        :placeholder="defaultLabelFor(form.provider, capability)"
                      />
                    </label>

                    <template v-if="selectedCatalog">
                      <div
                        v-for="field in fieldsForCapability(selectedCatalog, capability)"
                        :key="`cap-${capability}-${field.key}`"
                        class="provider-settings__field"
                      >
                      <label class="field-label">
                        {{ fieldLabel(field) }}<span
                          v-if="isFieldRequired(field, capability)"
                          class="provider-settings__required-mark"
                        >*</span>
                        <input
                          v-if="field.kind === 'checkbox'"
                          v-model="form.perCapability[capability].config[field.key]"
                          type="checkbox"
                        />
                        <template v-else-if="isCheckpointField(field)">
                          <div class="provider-settings__model-row">
                            <input
                              v-model="form.perCapability[capability].config[field.key]"
                              class="field-input"
                              type="text"
                              :placeholder="fieldPlaceholder(field)"
                              :required="isFieldRequired(field, capability)"
                              :list="`ckpt-${capability}-${field.key}`"
                              autocomplete="off"
                            />
                            <UiButton
                              size="sm"
                              variant="ghost"
                              type="button"
                              :loading="checkpointFetching[modelOptionsKey(capability, field.key)]"
                              @click="fetchCheckpointsFor(capability, field.key)"
                            >
                              {{ t('admin.providerSettings.comfyuiCheckpointFetch') }}
                            </UiButton>
                          </div>
                          <datalist :id="`ckpt-${capability}-${field.key}`">
                            <option
                              v-for="ckpt in (checkpointOptions[modelOptionsKey(capability, field.key)] ?? [])"
                              :key="ckpt"
                              :value="ckpt"
                            />
                          </datalist>
                        </template>
                        <template v-else-if="isModelField(field)">
                          <div class="provider-settings__model-row">
                            <UiCombobox
                              :model-value="String(form.perCapability[capability].config[field.key] ?? '')"
                              :options="modelOptions[modelOptionsKey(capability, field.key)] ?? []"
                              :loading="modelFetching[modelOptionsKey(capability, field.key)]"
                              :placeholder="fieldPlaceholder(field)"
                              :aria-label="fieldLabel(field)"
                              @update:model-value="form.perCapability[capability].config[field.key] = $event"
                            />
                            <UiButton
                              v-if="supportsModelDiscovery(selectedCatalog, capability)"
                              size="sm"
                              variant="ghost"
                              type="button"
                              :loading="modelFetching[modelOptionsKey(capability, field.key)]"
                              @click="fetchModelsFor(capability, field.key)"
                            >
                              {{ t('admin.providerSettings.actions.fetchModels') }}
                            </UiButton>
                          </div>
                        </template>
                        <input
                          v-else
                          v-model="form.perCapability[capability].config[field.key]"
                          class="field-input"
                          :type="fieldInputType(field.kind)"
                          :placeholder="fieldPlaceholder(field)"
                          :required="isFieldRequired(field, capability)"
                        />
                      </label>
                      <p v-if="fieldHint(field)" class="field-hint">{{ fieldHint(field) }}</p>
                      </div>
                    </template>

                    <div
                      v-if="capabilityProbes[capability]?.length"
                      class="provider-settings__probes"
                    >
                      <p class="provider-settings__probes-title">
                        {{ t('admin.providerSettings.probeResultsTitle') }}
                      </p>
                      <ul class="provider-settings__probe-list">
                        <li
                          v-for="(probe, index) in orderProbes(capabilityProbes[capability])"
                          :key="`${probe.capability}-${probe.action}-${index}`"
                          class="provider-settings__probe"
                        >
                          <UiBadge :variant="probe.ok ? 'success' : 'danger'">
                            {{ probeStatusMark(probe.ok) }}
                          </UiBadge>
                          <span class="provider-settings__probe-action">
                            {{ probeActionLabel(probe.action) }}
                          </span>
                          <span
                            v-if="formatLatency(probe.latency_ms)"
                            class="provider-settings__probe-latency"
                          >
                            {{ formatLatency(probe.latency_ms) }}
                          </span>
                          <span
                            v-if="probe.detail"
                            class="provider-settings__probe-detail"
                          >
                            {{ probe.detail }}
                          </span>
                        </li>
                      </ul>
                    </div>
                  </UiCard>
                </div>
              </div>
            </section>

            <!-- Footer summary + submit -->
            <div class="provider-settings__submit-bar">
              <p v-if="bulkSummary" class="provider-settings__bulk-summary">
                {{ bulkSummary }}
              </p>
              <UiButton
                type="submit"
                variant="primary"
                :loading="saving"
                :disabled="form.capabilities.length === 0"
              >
                {{ submitLabel }}
              </UiButton>
            </div>
          </form>
        </UiCard>
      </div>

      <!-- Right: existing connections grouped by capability ---------------- -->
      <section class="provider-settings__list">
        <div class="provider-settings__tabs">
          <button
            v-for="capability in capabilityTabs"
            :key="capability"
            type="button"
            class="provider-settings__tab"
            :class="{ 'is-active': activeCapability === capability }"
            @click="activeCapability = capability"
          >
            {{ capabilityLabel(capability) }}
          </button>
        </div>

        <p
          v-if="isSingleActiveTab && visibleConnections.length > 0"
          class="provider-settings__single-active-hint"
        >
          {{ t('admin.providerSettings.singleActiveHint') }}
        </p>

        <div v-if="visibleConnections.length === 0" class="provider-settings__empty">
          {{ t('admin.providerSettings.empty') }}
        </div>

        <UiCard
          v-for="row in visibleConnections"
          :key="row.id"
          class="provider-settings__connection"
        >
          <template #header>
            <div>
              <h3 class="provider-settings__connection-title">{{ connectionLabel(row.label) }}</h3>
              <p class="provider-settings__connection-meta">
                {{ providerLabel(row.provider) }}
              </p>
            </div>
            <UiBadge :variant="connectionBadge(row).variant">
              {{ connectionBadge(row).label }}
            </UiBadge>
          </template>

          <div class="provider-settings__chips">
            <span
              v-for="capability in row.capabilities"
              :key="capability"
              class="provider-settings__chip"
            >
              {{ capabilityLabel(capability) }}
            </span>
          </div>

          <p class="provider-settings__hint">
            {{ row.secret.configured
              ? t('admin.providerSettings.secretConfigured', { fingerprint: row.secret.fingerprint })
              : t('admin.providerSettings.secretMissing') }}
          </p>
          <p v-if="row.last_validation_error" class="provider-settings__error">
            {{ row.last_validation_error }}
          </p>

          <template #footer>
            <div class="provider-settings__actions">
              <UiButton size="sm" @click="startEdit(row)">
                {{ t('admin.providerSettings.actions.edit') }}
              </UiButton>
              <UiButton
                size="sm"
                :loading="testingId === row.id"
                @click="test(row)"
              >
                {{ t('admin.providerSettings.actions.test') }}
              </UiButton>
              <UiButton size="sm" variant="danger" @click="remove(row)">
                {{ t('admin.providerSettings.actions.delete') }}
              </UiButton>
            </div>
          </template>
        </UiCard>
      </section>
    </div>

    <RouterLink
      v-if="hasAnyProvider"
      to="/admin/models"
      class="provider-settings__next-step"
    >
      <UiCard hoverable>
        <template #header>
          <div class="provider-settings__next-step-header">
            <h2 class="provider-settings__card-title">
              {{ t('admin.page.providers.nextStep.title') }}
            </h2>
            <UiBadge variant="primary">{{ t('admin.page.providers.nextStep.action') }}</UiBadge>
          </div>
        </template>
        <p class="provider-settings__hint">
          {{ t('admin.page.providers.nextStep.description') }}
        </p>
      </UiCard>
    </RouterLink>
  </div>
</template>

<style scoped>
.provider-settings {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1280px;
}
.provider-settings__header,
.provider-settings__actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.provider-settings__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.provider-settings__subtitle,
.provider-settings__hint,
.provider-settings__connection-meta {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.provider-settings__hint--inline {
  margin: 0 0 var(--space-2);
}
.provider-settings__hint--empty {
  padding: var(--space-3);
  border: 1px dashed var(--color-border);
  border-radius: var(--card-radius);
}
.provider-settings__grid {
  display: grid;
  grid-template-columns: minmax(420px, 1fr) minmax(320px, 480px);
  gap: var(--space-4);
  align-items: start;
}
.provider-settings__next-step {
  color: inherit;
  text-decoration: none;
}
.provider-settings__next-step-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  width: 100%;
}
.provider-settings__form-column {
  min-width: 0;
}
.provider-settings__form-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.provider-settings__card-title,
.provider-settings__connection-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.provider-settings__form,
.provider-settings__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

/* Stepper layout — each step gets a left-rail index pill + body so the
   create-flow reads top-to-bottom even when sections wrap. */
.provider-settings__step {
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr);
  gap: var(--space-3);
  padding: var(--space-3) 0;
  border-top: 1px solid var(--color-border);
}
.provider-settings__step:first-of-type {
  border-top: none;
  padding-top: 0;
}
.provider-settings__step-index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 14px;
  background: rgba(139, 109, 255, 0.18);
  border: 1px solid rgba(139, 109, 255, 0.4);
  color: var(--color-text);
  font-size: var(--font-sm);
  font-weight: 600;
}
.provider-settings__step-body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
}
.provider-settings__step-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}

.provider-settings__checkbox {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-sm);
  color: var(--color-text);
}
.provider-settings__checkbox input {
  accent-color: var(--color-primary);
}
.provider-settings__view-spec-link {
  align-self: flex-start;
  text-decoration: none;
}
.provider-settings__capabilities,
.provider-settings__tabs,
.provider-settings__chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.provider-settings__capability,
.provider-settings__tab,
.provider-settings__chip {
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  padding: 6px 12px;
  cursor: pointer;
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}
.provider-settings__capability:hover:not(:disabled),
.provider-settings__tab:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}
.provider-settings__capability.is-active,
.provider-settings__tab.is-active {
  color: var(--color-text);
  border-color: rgba(139, 109, 255, 0.7);
  background: rgba(139, 109, 255, 0.22);
}
.provider-settings__capability.is-locked {
  opacity: 0.4;
  cursor: not-allowed;
}

/* Per-capability config cards live in a flex column so they stack
   cleanly on narrow viewports but can expand horizontally if we ever
   widen the form column. */
.provider-settings__cap-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.provider-settings__cap-card {
  background: rgba(139, 109, 255, 0.04);
  border: 1px solid rgba(139, 109, 255, 0.18);
}
.provider-settings__cap-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}
.provider-settings__cap-card-title {
  margin: 0;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--color-text);
}
.provider-settings__cap-card-actions {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

/* Inline live-probe results under a capability card. Each row: ok/fail
   badge · localized action label · latency · backend detail (muted). */
.provider-settings__probes {
  margin-top: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.provider-settings__probes-title {
  margin: 0;
  font-size: var(--font-xs);
  font-weight: 600;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.provider-settings__probe-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.provider-settings__probe {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: var(--space-2);
  font-size: var(--font-xs);
}
.provider-settings__probe-action {
  color: var(--color-text);
  font-weight: 600;
}
.provider-settings__probe-latency {
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}
.provider-settings__probe-detail {
  color: var(--color-text-secondary);
  flex: 1 1 100%;
  min-width: 0;
  line-height: 1.5;
}
.provider-settings__required-mark {
  margin-left: 2px;
  color: #ff7d6b;
  font-weight: 600;
}
.provider-settings__model-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.provider-settings__model-row .field-input {
  flex: 1;
  min-width: 0;
}

.provider-settings__submit-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}
.provider-settings__bulk-summary {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}

.provider-settings__connection {
  width: 100%;
}
.provider-settings__empty {
  padding: var(--space-5);
  border: 1px dashed var(--color-border);
  border-radius: var(--card-radius);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}
.provider-settings__single-active-hint {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-3);
  border-left: 3px solid var(--color-border);
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}
.provider-settings__error {
  margin: 0;
  color: #ffb4a8;
  font-size: var(--font-xs);
}

@media (max-width: 1080px) {
  .provider-settings__grid {
    grid-template-columns: 1fr;
  }
}
</style>
