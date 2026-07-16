<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { notification } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton, UiSection } from '@/components/ui'
import WorldEventFeedsPanel from '@/components/admin/WorldEventFeedsPanel.vue'
import {
  getAppSettingsGroup,
  putAppSettingsGroup,
  type CalendarRuntimeConfig,
  type FusionMaterialRuntimeConfig,
  type GeoIpRuntimeConfig,
  type NsfwRuntimeConfig,
  type WeatherRuntimeConfig,
  type WorldEventRuntimeConfig,
} from '@/utils/api/appSettings'

const { t } = useI18n()

const loading = ref(true)
const saving = reactive<Record<string, boolean>>({})

const weather = reactive<WeatherRuntimeConfig>({
  enabled: true,
  latitude: null,
  longitude: null,
  location_label: '',
  timezone_id: 'auto',
  cache_ttl_seconds: 900,
})
const calendar = reactive<CalendarRuntimeConfig>({ enabled: true, region: 'TW' })
const geoip = reactive<GeoIpRuntimeConfig>({
  enabled: true,
  provider: 'ip-api',
  endpoint: 'http://ip-api.com/json/',
  cache_ttl_seconds: 86400,
  timeout_seconds: 3,
})
const nsfw = reactive<NsfwRuntimeConfig>({ ttl_seconds: 1800 })
const worldEvents = reactive<WorldEventRuntimeConfig>({
  retention_days: 30,
  scheduler_interval_seconds: 3600,
})
const fusionMaterial = reactive<FusionMaterialRuntimeConfig>({
  ok_min_count: 3,
  ok_min_chars: 300,
  rich_min_count: 8,
  rich_min_chars: 1000,
})

function assign<T extends object>(target: T, src: T): void {
  Object.assign(target, src)
}

async function loadAll(): Promise<void> {
  loading.value = true
  try {
    const [w, c, g, n, we, fm] = await Promise.all([
      getAppSettingsGroup<WeatherRuntimeConfig>('weather'),
      getAppSettingsGroup<CalendarRuntimeConfig>('calendar'),
      getAppSettingsGroup<GeoIpRuntimeConfig>('geoip'),
      getAppSettingsGroup<NsfwRuntimeConfig>('nsfw'),
      getAppSettingsGroup<WorldEventRuntimeConfig>('world_events'),
      getAppSettingsGroup<FusionMaterialRuntimeConfig>('fusion_material'),
    ])
    assign(weather, w)
    assign(calendar, c)
    assign(geoip, g)
    assign(nsfw, n)
    assign(worldEvents, we)
    assign(fusionMaterial, fm)
  } catch (err) {
    notification.error({
      message: t('admin.siteSettings.loadFailed'),
      description: err instanceof Error ? err.message : String(err),
    })
  } finally {
    loading.value = false
  }
}

async function saveGroup(
  group: string,
  values: object,
): Promise<void> {
  saving[group] = true
  try {
    await putAppSettingsGroup(group, values)
    notification.success({ message: t('admin.siteSettings.saved'), duration: 2 })
  } catch (err) {
    const detail =
      (err as { response?: { data?: { detail?: string } } })?.response?.data
        ?.detail ?? (err instanceof Error ? err.message : String(err))
    notification.error({
      message: t('admin.siteSettings.saveFailed'),
      description: String(detail),
      duration: 6,
    })
  } finally {
    saving[group] = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="site-settings">
    <header class="site-settings__header">
      <div>
        <h1>{{ t('admin.siteSettings.title') }}</h1>
        <p class="site-settings__subtitle">{{ t('admin.siteSettings.subtitle') }}</p>
      </div>
      <UiBadge variant="primary">{{ t('admin.siteSettings.badge') }}</UiBadge>
    </header>

    <p v-if="loading" class="site-settings__loading">{{ t('common.loading') }}</p>

    <template v-else>
      <!-- Weather -->
      <UiSection :title="t('admin.siteSettings.weather.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.weather.hint') }}</p>
        <label class="field-label site-settings__check">
          <input v-model="weather.enabled" type="checkbox" />
          {{ t('admin.siteSettings.weather.enabled') }}
        </label>
        <div class="site-settings__grid">
          <label class="field-label">
            {{ t('admin.siteSettings.weather.latitude') }}
            <input v-model.number="weather.latitude" class="field-input" type="number" step="0.0001" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.weather.longitude') }}
            <input v-model.number="weather.longitude" class="field-input" type="number" step="0.0001" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.weather.label') }}
            <input
              v-model="weather.location_label"
              class="field-input"
              type="text"
              :placeholder="t('admin.siteSettings.weather.labelPlaceholder')"
            />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.weather.timezone') }}
            <input v-model="weather.timezone_id" class="field-input" type="text" placeholder="auto" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.weather.cacheTtl') }}
            <input v-model.number="weather.cache_ttl_seconds" class="field-input" type="number" min="60" />
          </label>
        </div>
        <UiButton variant="primary" :loading="saving.weather" @click="saveGroup('weather', weather)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- Calendar -->
      <UiSection :title="t('admin.siteSettings.calendar.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.calendar.hint') }}</p>
        <label class="field-label site-settings__check">
          <input v-model="calendar.enabled" type="checkbox" />
          {{ t('admin.siteSettings.calendar.enabled') }}
        </label>
        <label class="field-label">
          {{ t('admin.siteSettings.calendar.region') }}
          <input v-model="calendar.region" class="field-input" type="text" placeholder="TW" />
        </label>
        <UiButton variant="primary" :loading="saving.calendar" @click="saveGroup('calendar', calendar)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- GeoIP -->
      <UiSection :title="t('admin.siteSettings.geoip.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.geoip.hint') }}</p>
        <label class="field-label site-settings__check">
          <input v-model="geoip.enabled" type="checkbox" />
          {{ t('admin.siteSettings.geoip.enabled') }}
        </label>
        <div class="site-settings__grid">
          <label class="field-label">
            {{ t('admin.siteSettings.geoip.provider') }}
            <input v-model="geoip.provider" class="field-input" type="text" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.geoip.endpoint') }}
            <input v-model="geoip.endpoint" class="field-input" type="text" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.geoip.cacheTtl') }}
            <input v-model.number="geoip.cache_ttl_seconds" class="field-input" type="number" min="60" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.geoip.timeout') }}
            <input v-model.number="geoip.timeout_seconds" class="field-input" type="number" step="0.5" min="0.5" />
          </label>
        </div>
        <UiButton variant="primary" :loading="saving.geoip" @click="saveGroup('geoip', geoip)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- NSFW mode TTL -->
      <UiSection :title="t('admin.siteSettings.nsfw.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.nsfw.hint') }}</p>
        <label class="field-label">
          {{ t('admin.siteSettings.nsfw.ttl') }}
          <input v-model.number="nsfw.ttl_seconds" class="field-input" type="number" min="60" />
        </label>
        <UiButton variant="primary" :loading="saving.nsfw" @click="saveGroup('nsfw', nsfw)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- World-event policy -->
      <UiSection :title="t('admin.siteSettings.worldEvents.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.worldEvents.hint') }}</p>
        <div class="site-settings__grid">
          <label class="field-label">
            {{ t('admin.siteSettings.worldEvents.retention') }}
            <input v-model.number="worldEvents.retention_days" class="field-input" type="number" min="1" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.worldEvents.interval') }}
            <input v-model.number="worldEvents.scheduler_interval_seconds" class="field-input" type="number" min="60" />
          </label>
        </div>
        <UiButton variant="primary" :loading="saving.world_events" @click="saveGroup('world_events', worldEvents)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- Fusion material-richness badge thresholds (Creator Studio C1-P1) -->
      <UiSection :title="t('admin.siteSettings.fusionMaterial.title')" bordered>
        <p class="field-hint">{{ t('admin.siteSettings.fusionMaterial.hint') }}</p>
        <div class="site-settings__grid">
          <label class="field-label">
            {{ t('admin.siteSettings.fusionMaterial.okMinCount') }}
            <input v-model.number="fusionMaterial.ok_min_count" class="field-input" type="number" min="0" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.fusionMaterial.okMinChars') }}
            <input v-model.number="fusionMaterial.ok_min_chars" class="field-input" type="number" min="0" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.fusionMaterial.richMinCount') }}
            <input v-model.number="fusionMaterial.rich_min_count" class="field-input" type="number" min="0" />
          </label>
          <label class="field-label">
            {{ t('admin.siteSettings.fusionMaterial.richMinChars') }}
            <input v-model.number="fusionMaterial.rich_min_chars" class="field-input" type="number" min="0" />
          </label>
        </div>
        <UiButton variant="primary" :loading="saving.fusion_material" @click="saveGroup('fusion_material', fusionMaterial)">
          {{ t('admin.siteSettings.save') }}
        </UiButton>
      </UiSection>

      <!-- World-event RSS feeds CRUD (track 3) -->
      <WorldEventFeedsPanel />
    </template>
  </div>
</template>

<style scoped>
.site-settings {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 900px;
}
.site-settings__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.site-settings__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.site-settings__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.site-settings__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-3);
  margin: var(--space-2) 0 var(--space-3);
}
.site-settings__check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.site-settings__loading {
  color: var(--color-text-secondary);
}
</style>
