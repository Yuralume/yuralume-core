import axios from 'axios'

/**
 * Site-level runtime settings (CORE_ENV_TO_ADMIN_CONFIG track 2). Each
 * group is one JSON blob persisted in app_runtime_settings; the Admin
 * 站點設定 page reads/writes them here. Values are DB-authoritative once
 * saved; the backend falls back to env for an unset group.
 */

export interface WeatherRuntimeConfig {
  enabled: boolean
  latitude: number | null
  longitude: number | null
  location_label: string
  timezone_id: string
  cache_ttl_seconds: number
}

export interface CalendarRuntimeConfig {
  enabled: boolean
  region: string
}

export interface GeoIpRuntimeConfig {
  enabled: boolean
  provider: string
  endpoint: string
  cache_ttl_seconds: number
  timeout_seconds: number
}

export interface NsfwRuntimeConfig {
  ttl_seconds: number
}

export interface WorldEventRuntimeConfig {
  retention_days: number
  scheduler_interval_seconds: number
}

export interface CharacterFreezeRuntimeConfig {
  auto_freeze_enabled: boolean
  idle_days_threshold: number
}

/**
 * Fusion material-richness badge thresholds (Creator Studio C1-P1). Grades
 * a character's fusion-usable memory slice into rich / ok / sparse. Both
 * count and total-chars floors must be cleared to reach a tier. Purely a
 * hint — never blocks fusion-story creation.
 */
export interface FusionMaterialRuntimeConfig {
  ok_min_count: number
  ok_min_chars: number
  rich_min_count: number
  rich_min_chars: number
}

export async function getAppSettingsGroup<T>(group: string): Promise<T> {
  const { data } = await axios.get<{ group: string; values: T }>(
    `/api/v1/admin/app-settings/${group}`,
  )
  return data.values
}

export async function putAppSettingsGroup<T>(
  group: string,
  values: T,
): Promise<T> {
  const { data } = await axios.put<{ group: string; values: T }>(
    `/api/v1/admin/app-settings/${group}`,
    values,
  )
  return data.values
}
