<script setup lang="ts">
/**
 * Editable per-model price → live cost estimate for the usage report.
 *
 * Operators type each model's API price (per 1M tokens) and the cost is
 * recomputed in the browser from the aggregated input/output token
 * counts — no dependency on the server-side price JSON. Prices persist
 * in localStorage; this is a display-only estimate and never changes the
 * catalog cost the backend already recorded.
 */
import { computed, reactive } from 'vue'
import { useI18n } from 'vue-i18n'
import type { UsageModelBucket } from '@/utils/api/observability'
import { useLocale } from '@/composables/useLocale'
import {
  type PriceBook,
  type PriceEntry,
  computeCustomCost,
  hasPrice,
  loadPriceBook,
  priceBookKey,
  savePriceBook,
} from '@/utils/usagePricing'

const props = defineProps<{
  buckets: UsageModelBucket[]
  currency: string
}>()

const { t } = useI18n()
const { locale } = useLocale()

const storage: Storage | null =
  typeof window !== 'undefined' ? window.localStorage : null

const prices = reactive<PriceBook>(loadPriceBook(storage))

function entryFor(key: string): PriceEntry {
  if (!prices[key]) {
    prices[key] = { inputPerMillion: '', outputPerMillion: '' }
  }
  return prices[key]
}

function onPriceInput(
  key: string,
  field: 'inputPerMillion' | 'outputPerMillion',
  event: Event,
): void {
  entryFor(key)[field] = (event.target as HTMLInputElement).value
  savePriceBook(storage, prices)
}

function clearPrices(): void {
  for (const key of Object.keys(prices)) delete prices[key]
  savePriceBook(storage, prices)
}

interface CalcRow {
  key: string
  providerId: string
  modelId: string
  capability: string
  inputQuantity: number
  outputQuantity: number
  catalogCost: number
  customCost: number
  priced: boolean
}

const rows = computed<CalcRow[]>(() =>
  props.buckets.map((bucket) => {
    const key = priceBookKey(bucket.capability, bucket.provider_id, bucket.model_id)
    const entry = prices[key]
    return {
      key,
      providerId: bucket.provider_id,
      modelId: bucket.model_id,
      capability: bucket.capability,
      inputQuantity: bucket.total_input_quantity,
      outputQuantity: bucket.total_output_quantity,
      catalogCost: Number(bucket.total_cost_amount) || 0,
      customCost: computeCustomCost(
        bucket.total_input_quantity,
        bucket.total_output_quantity,
        entry,
      ),
      priced: hasPrice(entry),
    }
  }),
)

const totalCustom = computed(() =>
  rows.value.reduce((sum, row) => sum + row.customCost, 0),
)
const totalCatalog = computed(() =>
  rows.value.reduce((sum, row) => sum + row.catalogCost, 0),
)
const anyPriced = computed(() => rows.value.some((row) => row.priced))

function formatCost(amount: number): string {
  return new Intl.NumberFormat(locale.value, {
    style: 'currency',
    currency: props.currency || 'USD',
    maximumFractionDigits: 6,
  }).format(amount)
}

function formatQuantity(value: number): string {
  return new Intl.NumberFormat(locale.value).format(value)
}
</script>

<template>
  <section class="cost-calc">
    <div class="cost-calc__head">
      <div>
        <h4>{{ t('observabilityPanel.usage.priceCalc.title') }}</h4>
        <p class="cost-calc__hint">{{ t('observabilityPanel.usage.priceCalc.hint') }}</p>
      </div>
      <button
        type="button"
        class="cost-calc__clear"
        :disabled="!anyPriced"
        @click="clearPrices"
      >{{ t('observabilityPanel.usage.priceCalc.clear') }}</button>
    </div>

    <div class="cost-calc__scroll">
      <table class="cost-calc__table">
        <thead>
          <tr>
            <th>{{ t('observabilityPanel.usage.provider') }}</th>
            <th>{{ t('observabilityPanel.usage.model') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.inputTokens') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.outputTokens') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.inputPrice') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.outputPrice') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.customCost') }}</th>
            <th class="num">{{ t('observabilityPanel.usage.priceCalc.catalogCost') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.key">
            <td>{{ row.providerId || '-' }}</td>
            <td>
              {{ row.modelId || '-' }}
              <span class="cost-calc__cap">{{ row.capability }}</span>
            </td>
            <td class="num">{{ formatQuantity(row.inputQuantity) }}</td>
            <td class="num">{{ formatQuantity(row.outputQuantity) }}</td>
            <td class="num">
              <input
                class="field-input cost-calc__price"
                type="text"
                inputmode="decimal"
                :value="entryFor(row.key).inputPerMillion"
                :placeholder="'0'"
                :aria-label="t('observabilityPanel.usage.priceCalc.inputPrice')"
                @input="onPriceInput(row.key, 'inputPerMillion', $event)"
              />
            </td>
            <td class="num">
              <input
                class="field-input cost-calc__price"
                type="text"
                inputmode="decimal"
                :value="entryFor(row.key).outputPerMillion"
                :placeholder="'0'"
                :aria-label="t('observabilityPanel.usage.priceCalc.outputPrice')"
                @input="onPriceInput(row.key, 'outputPerMillion', $event)"
              />
            </td>
            <td class="num" :class="{ 'cost-calc__muted': !row.priced }">
              {{ row.priced ? formatCost(row.customCost) : '—' }}
            </td>
            <td class="num cost-calc__muted">{{ formatCost(row.catalogCost) }}</td>
          </tr>
          <tr v-if="rows.length === 0">
            <td colspan="8" class="cost-calc__empty">
              {{ t('observabilityPanel.usage.priceCalc.noData') }}
            </td>
          </tr>
        </tbody>
        <tfoot v-if="rows.length > 0">
          <tr>
            <td colspan="6" class="cost-calc__total-label">
              {{ t('observabilityPanel.usage.priceCalc.total') }}
            </td>
            <td class="num cost-calc__total">{{ formatCost(totalCustom) }}</td>
            <td class="num cost-calc__muted">{{ formatCost(totalCatalog) }}</td>
          </tr>
        </tfoot>
      </table>
    </div>
    <p class="cost-calc__note">{{ t('observabilityPanel.usage.priceCalc.unitNote') }}</p>
  </section>
</template>

<style scoped>
.cost-calc {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.cost-calc__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.cost-calc__head h4 {
  margin: 0;
  font-size: 13px;
}
.cost-calc__hint {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.cost-calc__clear {
  flex-shrink: 0;
  padding: 4px 10px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
}
.cost-calc__clear:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.cost-calc__clear:not(:disabled):hover {
  border-color: var(--color-primary);
  color: var(--color-primary-light);
}
.cost-calc__scroll {
  overflow-x: auto;
}
.cost-calc__table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.cost-calc__table th,
.cost-calc__table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}
.cost-calc__table th.num,
.cost-calc__table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.cost-calc__cap {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
  font-size: 11px;
}
.cost-calc__price {
  width: 92px;
  padding: 4px 6px;
  text-align: right;
}
.cost-calc__muted {
  color: var(--color-text-secondary);
}
.cost-calc__total-label {
  text-align: right;
  font-weight: 600;
}
.cost-calc__total {
  font-weight: 700;
  color: var(--color-primary-light);
}
.cost-calc__empty {
  text-align: center;
  color: var(--color-text-secondary);
  padding: 12px;
}
.cost-calc__note {
  margin: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
</style>
