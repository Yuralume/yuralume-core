<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  listDispositionDrift,
  type DispositionDriftRecord,
} from '@/utils/api/observability'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'
import { UiBadge, UiButton } from '@/components/ui'

const props = defineProps<{ characterId: string }>()
const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()

const records = ref<DispositionDriftRecord[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const DIMENSION_LABEL_KEY: Record<DispositionDriftRecord['dimension'], string> = {
  self_centeredness: 'admin.dispositionDrift.dimension.selfCenteredness',
  candor: 'admin.dispositionDrift.dimension.candor',
  sharing_drive: 'admin.dispositionDrift.dimension.sharingDrive',
  associativeness: 'admin.dispositionDrift.dimension.associativeness',
}

const BAND_LABEL_KEY: Record<DispositionDriftRecord['from_band'], string> = {
  low: 'admin.dispositionDrift.band.low',
  medium: 'admin.dispositionDrift.band.medium',
  high: 'admin.dispositionDrift.band.high',
}

const BAND_VARIANT: Record<
  DispositionDriftRecord['to_band'],
  'default' | 'primary' | 'success' | 'warning'
> = {
  low: 'default',
  medium: 'primary',
  high: 'warning',
}

async function load() {
  if (!props.characterId) {
    records.value = []
    return
  }
  loading.value = true
  error.value = null
  try {
    records.value = await listDispositionDrift({
      characterId: props.characterId,
      limit: 30,
    })
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('admin.dispositionDrift.loadFailed')
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => void load())
onMounted(load)
</script>

<template>
  <section class="drift-history">
    <header class="drift-history__header">
      <div>
        <p class="drift-history__hint">
          {{ t('admin.dispositionDrift.hint') }}
        </p>
      </div>
      <UiButton variant="ghost" size="sm" :loading="loading" @click="load">
        {{ t('common.actions.refresh') }}
      </UiButton>
    </header>

    <p v-if="loading && records.length === 0" class="drift-history__status">
      {{ t('common.state.loading') }}
    </p>
    <p v-else-if="error" class="drift-history__status drift-history__status--error">
      {{ error }}
    </p>
    <p v-else-if="records.length === 0" class="drift-history__status">
      {{ t('admin.dispositionDrift.empty') }}
    </p>

    <ol v-else class="drift-history__timeline">
      <li v-for="record in records" :key="record.id" class="drift-history__item">
        <div class="drift-history__item-head">
          <span class="drift-history__dimension">
            {{ t(DIMENSION_LABEL_KEY[record.dimension]) }}
          </span>
          <span class="drift-history__shift">
            <UiBadge variant="default">{{ t(BAND_LABEL_KEY[record.from_band]) }}</UiBadge>
            <span class="drift-history__arrow" aria-hidden="true">→</span>
            <UiBadge :variant="BAND_VARIANT[record.to_band]">
              {{ t(BAND_LABEL_KEY[record.to_band]) }}
            </UiBadge>
          </span>
          <time class="drift-history__time">{{ formatDateTime(record.decided_at, locale, timeZone) }}</time>
        </div>
        <p class="drift-history__reason">{{ record.reason }}</p>
        <blockquote v-if="record.evidence_quote" class="drift-history__evidence">
          {{ t('admin.dispositionDrift.evidenceQuote', { quote: record.evidence_quote }) }}
        </blockquote>
      </li>
    </ol>
  </section>
</template>

<style scoped>
.drift-history {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.drift-history__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.drift-history__hint {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.drift-history__status {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.drift-history__status--error {
  color: var(--color-danger, #ff6b6b);
}
.drift-history__timeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.drift-history__item {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface-2);
}
.drift-history__item-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.drift-history__dimension {
  font-weight: 600;
  font-size: var(--font-md);
}
.drift-history__shift {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
}
.drift-history__arrow {
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}
.drift-history__time {
  margin-left: auto;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}
.drift-history__reason {
  margin: 0;
  font-size: var(--font-sm);
  line-height: 1.6;
}
.drift-history__evidence {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-left: 3px solid var(--color-border);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  font-style: italic;
}
</style>
