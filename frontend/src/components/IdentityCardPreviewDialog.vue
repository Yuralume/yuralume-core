<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'
import { UiButton } from '@/components/ui'
import type { IdentityCard } from '@/utils/api/identityCards'
import { IDENTITY_CARD_PREVIEW_FIELDS, identityCardPreviewCell } from '@/utils/identityCardPreview'

/**
 * 設定頁「玩家身分卡」管理面的唯讀預覽（IC3）——第一版沒有內容編輯，這裡
 * 只渲染，不收任何輸入。全部 12 欄位的順序與標籤鍵定義在
 * `identityCardPreview.ts`，欄位標籤沿用精靈既有的 i18n 鍵，不另造一套。
 *
 * 版面比照 `PlayerPersonaNoteModal.vue` 的既有 overlay 先例（Teleport +
 * backdrop + role="dialog"），含同一套 bindEscape／unbindEscape 慣例——少了
 * 這段，開著這個浮窗按 Esc 什麼都不會發生，玩家只能找那顆小的 × 或點背景
 * 才關得掉。
 */
const props = defineProps<{
  card: IdentityCard | null
}>()

const emit = defineEmits<{
  close: []
}>()

const { t } = useI18n()

watch(() => props.card, (card, previousCard) => {
  if (card && !previousCard) bindEscape()
  else if (!card && previousCard) unbindEscape()
})

onBeforeUnmount(unbindEscape)

function bindEscape() {
  if (typeof window === 'undefined') return
  window.addEventListener('keydown', handleWindowKeydown)
}

function unbindEscape() {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', handleWindowKeydown)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
}

interface PreviewRow {
  key: string
  label: string
  value: string
}

const rows = computed<PreviewRow[]>(() => {
  const card = props.card
  if (!card) return []
  return IDENTITY_CARD_PREVIEW_FIELDS.map((field) => {
    const cell = identityCardPreviewCell(field, card)
    const value = cell.kind === 'text'
      ? (cell.value || t('common.fallback.notSet'))
      : t(cell.key)
    return { key: field.field, label: t(field.labelKey), value }
  })
})
</script>

<template>
  <Teleport to="body">
    <div
      v-if="card"
      class="identity-card-preview__backdrop"
      @click.self="emit('close')"
    >
      <section
        class="identity-card-preview"
        role="dialog"
        aria-modal="true"
        aria-labelledby="identity-card-preview-title"
      >
        <header class="identity-card-preview__header">
          <h3 id="identity-card-preview-title" class="identity-card-preview__title">
            {{ card.name }}
          </h3>
          <UiButton
            variant="ghost"
            size="sm"
            :aria-label="t('common.actions.close')"
            @click="emit('close')"
          >
            <CloseOutlined aria-hidden="true" />
          </UiButton>
        </header>

        <dl class="identity-card-preview__rows">
          <template v-for="row in rows" :key="row.key">
            <dt class="identity-card-preview__label">{{ row.label }}</dt>
            <dd class="identity-card-preview__value">{{ row.value }}</dd>
          </template>
        </dl>

        <footer class="identity-card-preview__actions">
          <UiButton variant="secondary" size="sm" @click="emit('close')">
            {{ t('common.actions.close') }}
          </UiButton>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.identity-card-preview__backdrop {
  position: fixed;
  inset: 0;
  height: 100dvh;
  z-index: 920;
  display: flex;
  align-items: center;
  justify-content: center;
  padding:
    max(24px, var(--safe-area-top))
    max(24px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(24px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(5px);
}

.identity-card-preview {
  width: min(520px, calc(100vw - 32px));
  max-height: min(92dvh, 720px);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  border: 1px solid rgba(64, 156, 255, 0.34);
  border-radius: 8px;
  padding: var(--space-4);
  background: rgba(24, 33, 50, 0.98);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.46);
  overflow-y: auto;
}

.identity-card-preview__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.identity-card-preview__title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-lg);
  font-weight: 650;
}

.identity-card-preview__rows {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  margin: 0;
}

.identity-card-preview__label {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  font-weight: 600;
}

.identity-card-preview__value {
  margin: 2px 0 0;
  color: var(--color-text);
  font-size: var(--font-sm);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.identity-card-preview__actions {
  display: flex;
  justify-content: flex-end;
}
</style>
