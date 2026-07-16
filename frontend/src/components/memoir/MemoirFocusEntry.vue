<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

import { UiBadge, UiButton } from '@/components/ui'
import { useTimezone } from '@/composables/useTimezone'
import {
  timeInputValueForTimezone,
  todayISOForTimezone,
} from '@/i18n/formatters'
import { splitMemoirSummary } from '@/utils/memoirSentence'
import type { MemoirEntry, MemoirEntryKind } from '@/utils/api/memoir'

const props = defineProps<{
  entry: MemoirEntry | null
  position: { current: number, total: number }
  busy?: boolean
}>()

const emit = defineEmits<{
  (event: 'pin', kind: MemoirEntryKind, entryId: string): void
  (event: 'unpin', kind: MemoirEntryKind, entryId: string): void
  (event: 'prev'): void
  (event: 'next'): void
  // This component has no characterId, so "寫成番外" bubbles the entry up
  // to the host (MemoirContent) which owns the id + router handoff.
  (event: 'write-extra', entry: MemoirEntry): void
}>()

const { t, locale } = useI18n()
const { timeZone } = useTimezone()

const kindVariant = computed<'primary' | 'success' | 'default'>(() => {
  if (!props.entry) return 'default'
  if (props.entry.kind === 'milestone') return 'success'
  if (props.entry.kind === 'emotion') return 'primary'
  return 'default'
})

const kindLabel = computed(() => {
  if (!props.entry) return ''
  return t(`memoir.kind.${props.entry.kind}`)
})

/**
 * 切日期 + 時間段（可選）。後端只給單一 timestamp，這裡把它拆成
 *   `2026.05.31`
 *   `(星期六)`
 *   `20:30`
 * 三段，讓書頁的小標看起來像日記簿一樣有起點。
 */
const dateInfo = computed(() => {
  if (!props.entry) return null
  try {
    const dt = new Date(props.entry.occurred_at)
    const date = todayISOForTimezone(timeZone.value, dt).replaceAll('-', '.')
    const weekday = new Intl.DateTimeFormat(locale.value, {
      weekday: 'long',
      timeZone: timeZone.value,
    }).format(dt)
    // Full-width brackets read naturally for CJK locales but look off
    // wrapping a Latin weekday; pick the bracket family by locale.
    const isCjk = locale.value.startsWith('zh') || locale.value.startsWith('ja')
    return {
      date,
      weekday: isCjk ? `（${weekday}）` : `(${weekday})`,
      time: timeInputValueForTimezone(props.entry.occurred_at, timeZone.value),
    }
  }
  catch {
    return null
  }
})

const scoreLabel = computed(() => {
  if (!props.entry) return ''
  return t('memoir.timeline.score', { value: props.entry.score.toFixed(2) })
})

/**
 * 把 entry.summary 拆成 title / paragraphs / italic-coda（情緒尾韻）。
 * 拆句邏輯抽到 `@/utils/memoirSentence`，同時支援 CJK（。！？）與拉丁
 * （.!?）句末符與顯示寬度門檻，讓英文/日文回憶錄也能正確斷句抽標題。
 */
const body = computed(() => splitMemoirSummary(props.entry?.summary ?? ''))

const tags = computed(() => {
  const raw = props.entry?.extras.tags
  if (!raw) return []
  return raw.split(',').map(s => s.trim()).filter(Boolean)
})

const emotionMeta = computed(() => {
  const extras = props.entry?.extras ?? {}
  const valence = extras.valence
  if (!valence) return null
  return {
    valence,
    arousal: extras.arousal,
    label: extras.emotion_label,
  }
})

const togglePin = () => {
  const e = props.entry
  if (!e || props.busy) return
  if (e.pinned) emit('unpin', e.kind, e.entry_id)
  else emit('pin', e.kind, e.entry_id)
}
</script>

<template>
  <div class="focus-block">
    <div class="focus-book">
      <div class="focus-book__backdrop" aria-hidden="true" />
      <div class="focus-book__pen" aria-hidden="true" />
      <div class="focus-book__sparkle focus-book__sparkle--tl" aria-hidden="true" />
      <div class="focus-book__sparkle focus-book__sparkle--tr" aria-hidden="true" />

      <div v-if="entry" class="focus-book__page">
        <header class="focus-book__head">
          <div class="focus-book__date-block">
            <span v-if="dateInfo" class="focus-book__date">{{ dateInfo.date }}</span>
            <span v-if="dateInfo" class="focus-book__weekday">{{ dateInfo.weekday }}</span>
            <span v-if="dateInfo" class="focus-book__time">{{ dateInfo.time }}</span>
          </div>
          <span class="focus-book__spacer" />
          <span
            v-if="entry.pinned"
            class="focus-book__pin-mark"
            aria-hidden="true"
            :title="t('memoir.timeline.unpin')"
          />
          <UiBadge :variant="kindVariant">{{ kindLabel }}</UiBadge>
          <span class="focus-book__score" :title="scoreLabel">
            <span class="focus-book__score-label">{{ t('memoir.focus.intensityLabel') }}</span>
            <span class="focus-book__score-value">{{ entry.score.toFixed(2) }}</span>
          </span>
        </header>

        <h3 v-if="body.title" class="focus-book__title">{{ body.title }}</h3>

        <div class="focus-book__scroll">
          <p
            v-for="(para, idx) in body.paragraphs"
            :key="idx"
            class="focus-book__paragraph"
          >
            {{ para }}
          </p>

          <p v-if="body.coda" class="focus-book__coda">{{ body.coda }}</p>

          <div v-if="emotionMeta" class="focus-book__emotion">
            {{ t('memoir.focus.emotionMeta', {
              label: emotionMeta.label,
              valence: emotionMeta.valence,
              arousal: emotionMeta.arousal,
            }) }}
          </div>

          <div v-if="tags.length" class="focus-book__tags">
            <span
              v-for="tag in tags"
              :key="tag"
              class="focus-book__tag"
            >#{{ tag }}</span>
          </div>
        </div>
      </div>

      <div v-else class="focus-book__placeholder">
        {{ t('memoir.focus.empty') }}
      </div>
    </div>

    <div class="focus-book__actions">
      <UiButton
        :variant="entry?.pinned ? 'secondary' : 'primary'"
        size="md"
        :disabled="!entry"
        :loading="busy"
        @click="togglePin"
      >
        {{ entry?.pinned ? t('memoir.timeline.unpin') : t('memoir.focus.pinThis') }}
      </UiButton>

      <UiButton
        variant="secondary"
        size="md"
        :disabled="!entry"
        @click="entry && emit('write-extra', entry)"
      >
        {{ t('memoir.writeExtra.label') }}
      </UiButton>

      <div class="focus-book__nav">
        <button
          type="button"
          class="focus-book__nav-btn"
          :disabled="position.total === 0 || position.current <= 1"
          :aria-label="t('memoir.focus.prev')"
          @click="emit('prev')"
        >
          ‹
        </button>
        <span class="focus-book__tassel" aria-hidden="true">
          <span class="focus-book__tassel-chain" />
          <span class="focus-book__tassel-bead">
            {{ position.current }} / {{ position.total }}
          </span>
          <span class="focus-book__tassel-chain" />
        </span>
        <button
          type="button"
          class="focus-book__nav-btn"
          :disabled="position.total === 0 || position.current >= position.total"
          :aria-label="t('memoir.focus.next')"
          @click="emit('next')"
        >
          ›
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.focus-block {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-3);
}

.focus-book {
  position: relative;
  width: 100%;
  max-width: 620px;
  aspect-ratio: 1164 / 1038;
  margin: 0 auto;
}
.focus-book__backdrop {
  position: absolute;
  inset: 0;
  background-image: url('/memoir/book_main.png');
  background-position: center;
  background-repeat: no-repeat;
  background-size: contain;
  pointer-events: none;
  filter: drop-shadow(0 22px 30px rgba(0, 0, 0, 0.55));
}

/* 羽毛筆道具：擺在書本右下角，斜斜壓在書邊，做「剛寫完」的感覺 */
.focus-book__pen {
  position: absolute;
  right: -32px;
  bottom: 6%;
  width: 120px;
  height: 150px;
  background-image: url('/memoir/pen.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: bottom right;
  transform: rotate(18deg);
  pointer-events: none;
  z-index: 2;
  opacity: 0.95;
  filter: drop-shadow(0 6px 12px rgba(0, 0, 0, 0.5));
}

/* 書本上角的點綴星，呼應背景星空 */
.focus-book__sparkle {
  position: absolute;
  background-image: url('/memoir/star_purple.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  pointer-events: none;
  z-index: 2;
  opacity: 0.75;
  filter: drop-shadow(0 0 6px rgba(196, 181, 253, 0.6));
}
.focus-book__sparkle--tl {
  top: 4%;
  left: 6%;
  width: 16px;
  height: 16px;
}
.focus-book__sparkle--tr {
  top: 10%;
  right: 8%;
  width: 12px;
  height: 12px;
  opacity: 0.55;
}

.focus-book__page {
  position: absolute;
  inset: 18% 16% 30% 12%;
  display: flex;
  flex-direction: column;
  gap: 10px;
  color: #2d2138;
  overflow: hidden;
}
.focus-book__head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 11px;
  color: #6b4e80;
  flex-shrink: 0;
  padding-bottom: 6px;
  border-bottom: 1px dashed rgba(139, 92, 246, 0.25);
}
.focus-book__date-block {
  display: flex;
  align-items: baseline;
  gap: 4px;
}
.focus-book__date {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: #4a356b;
  letter-spacing: 0.04em;
}
.focus-book__weekday {
  color: #8a6fab;
  font-size: 10px;
}
.focus-book__time {
  font-variant-numeric: tabular-nums;
  color: #7c5cb8;
  margin-left: 6px;
  letter-spacing: 0.05em;
}
.focus-book__spacer {
  flex: 1;
}
.focus-book__pin-mark {
  display: inline-block;
  width: 18px;
  height: 18px;
  background-image: url('/memoir/star_gold.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  filter: drop-shadow(0 0 6px rgba(255, 215, 100, 0.7));
}
.focus-book__score {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(139, 92, 246, 0.12);
  border: 1px solid rgba(139, 92, 246, 0.32);
  font-variant-numeric: tabular-nums;
}
.focus-book__score-label {
  font-size: 9px;
  color: #6b4e80;
  letter-spacing: 0.05em;
}
.focus-book__score-value {
  font-weight: 700;
  color: #4a356b;
  font-size: 11px;
}

.focus-book__title {
  margin: 0;
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
  font-weight: 700;
  font-size: 18px;
  color: #2d2138;
  letter-spacing: 0.02em;
  line-height: 1.4;
  flex-shrink: 0;
}

.focus-book__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
  scrollbar-width: thin;
  scrollbar-color: rgba(120, 80, 160, 0.4) transparent;
}
.focus-book__scroll::-webkit-scrollbar {
  width: 6px;
}
.focus-book__scroll::-webkit-scrollbar-thumb {
  background: rgba(120, 80, 160, 0.4);
  border-radius: 999px;
}

.focus-book__paragraph {
  margin: 0;
  font-size: 14px;
  line-height: 1.85;
  color: #2d2138;
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
  text-indent: 2em;
  white-space: pre-wrap;
  word-break: break-word;
}

.focus-book__coda {
  margin: 6px 0 0;
  padding: 6px 10px;
  font-size: 12px;
  line-height: 1.7;
  color: #7c5cb8;
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
  font-style: italic;
  border-left: 2px solid rgba(139, 92, 246, 0.35);
  background: rgba(139, 92, 246, 0.05);
  border-radius: 0 4px 4px 0;
}

.focus-book__emotion {
  font-size: 11px;
  color: #6b4e80;
  font-style: italic;
}

.focus-book__tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 4px;
}
.focus-book__tag {
  font-size: 10px;
  color: #7c5cb8;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(139, 92, 246, 0.10);
  letter-spacing: 0.03em;
}

.focus-book__placeholder {
  position: absolute;
  inset: 30% 18% 35% 18%;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  font-size: var(--font-sm);
  color: #6b4e80;
  font-style: italic;
}

.focus-book__actions {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}
.focus-book__nav {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.focus-book__nav-btn {
  appearance: none;
  background:
    linear-gradient(180deg, rgba(139, 92, 246, 0.18), rgba(91, 81, 158, 0.12));
  border: 1px solid rgba(139, 92, 246, 0.4);
  border-radius: 50%;
  color: var(--color-text);
  width: 32px;
  height: 32px;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s, border-color 0.15s, transform 0.15s;
}
.focus-book__nav-btn:hover:not(:disabled) {
  background:
    linear-gradient(180deg, rgba(139, 92, 246, 0.32), rgba(91, 81, 158, 0.22));
  transform: translateY(-1px);
}
.focus-book__nav-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

/* 墜飾風分頁器：左右兩段細鏈 + 中央橢圓墜珠 */
.focus-book__tassel {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-variant-numeric: tabular-nums;
}
.focus-book__tassel-chain {
  display: inline-block;
  width: 18px;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(196, 181, 253, 0.6),
    transparent
  );
}
.focus-book__tassel-bead {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 4px 14px;
  border-radius: 999px;
  background:
    linear-gradient(135deg, rgba(196, 181, 253, 0.22), rgba(139, 92, 246, 0.18));
  border: 1px solid rgba(196, 181, 253, 0.5);
  color: var(--color-text);
  font-size: 13px;
  letter-spacing: 0.04em;
  box-shadow: 0 0 8px rgba(139, 92, 246, 0.25);
}

@media (max-width: 720px) {
  .focus-book {
    max-width: 100%;
  }
  .focus-book__page {
    inset: 17% 14% 28% 12%;
  }
  .focus-book__title {
    font-size: 16px;
  }
  .focus-book__paragraph {
    font-size: 13px;
    line-height: 1.7;
  }
}
</style>
