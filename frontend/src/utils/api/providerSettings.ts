import axios from 'axios'

export interface ProviderFieldSpec {
  key: string
  label: string
  kind: string
  required: boolean
  required_for_capabilities: string[]
  placeholder: string
  secret: boolean
  advanced: boolean
}

export interface ProviderCatalogEntry {
  id: string
  display_name: string
  capabilities: string[]
  auth_fields: ProviderFieldSpec[]
  config_fields: ProviderFieldSpec[]
  model_catalog_mode: string
  default_models: string[]
  adapter_kind: string
  docs_url: string
}

export interface ProviderSecretState {
  configured: boolean
  fingerprint: string
}

export interface ProviderConnection {
  id: string
  provider: string
  label: string
  enabled: boolean
  capabilities: string[]
  config: Record<string, unknown>
  secret: ProviderSecretState
  last_validated_at: string | null
  last_validation_error: string | null
  created_at: string | null
  updated_at: string | null
}

export interface ProviderConnectionPayload {
  provider: string
  label: string
  enabled: boolean
  capabilities: string[]
  config: Record<string, unknown>
  secret: Record<string, unknown>
}

export interface ProviderConnectionTestResult {
  ok: boolean
  last_validated_at: string | null
  last_validation_error: string | null
}

export async function listProviderCatalog(): Promise<ProviderCatalogEntry[]> {
  const { data } = await axios.get<ProviderCatalogEntry[]>(
    '/api/v1/admin/providers/catalog',
  )
  return data
}

export async function listProviderConnections(): Promise<ProviderConnection[]> {
  const { data } = await axios.get<ProviderConnection[]>(
    '/api/v1/admin/providers',
  )
  return data
}

export async function createProviderConnection(
  payload: ProviderConnectionPayload,
): Promise<ProviderConnection> {
  const { data } = await axios.post<ProviderConnection>(
    '/api/v1/admin/providers',
    payload,
  )
  return data
}

export async function updateProviderConnection(
  id: string,
  payload: Partial<ProviderConnectionPayload> & { clear_secret?: boolean },
): Promise<ProviderConnection> {
  const { data } = await axios.patch<ProviderConnection>(
    `/api/v1/admin/providers/${encodeURIComponent(id)}`,
    payload,
  )
  return data
}

export async function deleteProviderConnection(id: string): Promise<void> {
  await axios.delete(`/api/v1/admin/providers/${encodeURIComponent(id)}`)
}

export async function testProviderConnection(id: string): Promise<ProviderConnection> {
  const { data } = await axios.post<ProviderConnection>(
    `/api/v1/admin/providers/${encodeURIComponent(id)}/test`,
  )
  return data
}

export async function testDraftProviderConnection(
  payload: ProviderConnectionPayload,
): Promise<ProviderConnectionTestResult> {
  const { data } = await axios.post<ProviderConnectionTestResult>(
    '/api/v1/admin/providers/test-draft',
    payload,
  )
  return data
}

export interface ListProviderModelsRequest {
  provider: string
  capability: string
  config: Record<string, unknown>
  secret: Record<string, unknown>
  connection_id?: string | null
}

export interface ListProviderModelsResponse {
  models: string[]
  error: string | null
}

export async function listProviderModels(
  payload: ListProviderModelsRequest,
): Promise<ListProviderModelsResponse> {
  const { data } = await axios.post<ListProviderModelsResponse>(
    '/api/v1/admin/providers/list-models',
    payload,
  )
  return data
}

export interface ComfyCheckpointList {
  available: boolean
  checkpoints: string[]
  error: string
}

/**
 * Fetch the checkpoint filenames a ComfyUI server advertises, for the
 * checkpoint dropdown in the ComfyUI provider form. Never throws a
 * populated list on failure: an unreachable ComfyUI returns
 * ``available=false`` so the form can fall back to free-text entry
 * (CORE_ENV_TO_ADMIN_CONFIG Phase 2 risk note).
 */
export async function listComfyuiCheckpoints(
  server: string,
): Promise<ComfyCheckpointList> {
  const { data } = await axios.get<ComfyCheckpointList>(
    '/api/v1/system/comfyui/checkpoints',
    { params: { server } },
  )
  return data
}
