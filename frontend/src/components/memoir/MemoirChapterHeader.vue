<script setup lang="ts">
/**
 * 章節 header — 用「dream pose 手札卡」呈現最新一篇 SelfReflection。
 *
 * 視覺意圖：把章節敘事打扮成一張貼在書本上方的便籤 —
 *   - 左側大引號 ❝
 *   - narrative 用 serif 手寫感字體
 *   - 右上角 dream pose 印章式徽章
 *   - 底部一條 themes chip + 時間範圍小字
 * 多章節時上方有 pill 切換鈕。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { MemoirChapter } from '@/utils/api/memoir'

const props = defineProps<{
  chapters: MemoirChapter[]
}>()

const { t } = useI18n()

const activeIndex = ref(0)

watch(
  () => props.chapters.length,
  (count) => {
    if (activeIndex.value >= count) activeIndex.value = 0
  },
)

const activeChapter = computed(() => props.chapters[activeIndex.value] ?? null)

const periodLabel = (period: 'week' | 'month') => t(
  period === 'week' ? 'memoir.chapters.periodWeek' : 'memoir.chapters.periodMonth',
)

const dateRange = computed(() => {
  const c = activeChapter.value
  if (!c) return ''
  return t('memoir.chapters.periodRange', { start: c.period_start, end: c.period_end })
})
</script>

<template>
  <header v-if="activeChapter" class="chapter-card">
    <span class="chapter-card__sparkle chapter-card__sparkle--tl" aria-hidden="true" />
    <span class="chapter-card__sparkle chapter-card__sparkle--br" aria-hidden="true" />

    <div v-if="chapters.length > 1" class="chapter-card__tabs">
      <button
        v-for="(chapter, idx) in chapters"
        :key="chapter.period"
        type="button"
        class="chapter-card__tab"
        :class="{ 'chapter-card__tab--active': idx === activeIndex }"
        @click="activeIndex = idx"
      >
        {{ periodLabel(chapter.period) }}
      </button>
    </div>

    <div class="chapter-card__body">
      <div class="chapter-card__quote-glyph" aria-hidden="true">❝</div>
      <p class="chapter-card__narrative">{{ activeChapter.narrative }}</p>
      <span class="chapter-card__stamp">
        <span class="chapter-card__stamp-label">dream</span>
        <span class="chapter-card__stamp-sub">pose</span>
      </span>
      <span class="chapter-card__pen" aria-hidden="true" />
    </div>

    <footer class="chapter-card__foot">
      <span class="chapter-card__period">
        {{ periodLabel(activeChapter.period) }} · {{ dateRange }}
      </span>
      <span
        v-for="theme in activeChapter.dominant_themes"
        :key="theme"
        class="chapter-card__theme"
      >#{{ theme }}</span>
    </footer>
  </header>

  <header v-else class="chapter-card chapter-card--empty">
    <p class="chapter-card__empty">{{ t('memoir.chapters.empty') }}</p>
  </header>
</template>

<style scoped>
.chapter-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 18px 24px 16px;
  border-radius: 14px;
  background:
    linear-gradient(
      135deg,
      rgba(139, 92, 246, 0.18),
      rgba(91, 157, 255, 0.10) 55%,
      rgba(120, 80, 200, 0.06)
    );
  border: 1px solid rgba(139, 92, 246, 0.32);
  box-shadow:
    0 4px 18px rgba(0, 0, 0, 0.25),
    inset 0 0 24px rgba(139, 92, 246, 0.08);
}
/* 紙質格線：模擬手札紙背景的橫線 */
.chapter-card::before {
  content: '';
  position: absolute;
  inset: 18px 60px 18px 60px;
  background-image: repeating-linear-gradient(
    180deg,
    transparent 0,
    transparent 27px,
    rgba(196, 181, 253, 0.08) 27px,
    rgba(196, 181, 253, 0.08) 28px
  );
  pointer-events: none;
  border-radius: 6px;
}
.chapter-card--empty {
  background: rgba(255, 255, 255, 0.02);
  border-color: var(--color-border);
  box-shadow: none;
  padding: var(--space-3) var(--space-4);
}
.chapter-card--empty::before {
  display: none;
}

.chapter-card__sparkle {
  position: absolute;
  width: 16px;
  height: 16px;
  background-image: url('/memoir/star_purple.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  opacity: 0.7;
  pointer-events: none;
  filter: drop-shadow(0 0 6px rgba(196, 181, 253, 0.6));
}
.chapter-card__sparkle--tl {
  top: 6px;
  left: 8px;
  width: 12px;
  height: 12px;
}
.chapter-card__sparkle--br {
  bottom: 8px;
  right: 90px;
  width: 14px;
  height: 14px;
  opacity: 0.6;
}

.chapter-card__pen {
  position: absolute;
  right: -8px;
  bottom: -12px;
  width: 80px;
  height: 100px;
  background-image: url('/memoir/pen.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: bottom right;
  transform: rotate(8deg);
  opacity: 0.85;
  pointer-events: none;
  filter: drop-shadow(0 4px 8px rgba(0, 0, 0, 0.4));
}

.chapter-card__tabs {
  position: relative;
  display: flex;
  gap: 4px;
}
.chapter-card__tab {
  appearance: none;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid rgba(139, 92, 246, 0.25);
  border-radius: 999px;
  color: var(--color-text-secondary);
  font-size: 11px;
  padding: 3px 12px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
  letter-spacing: 0.05em;
}
.chapter-card__tab:hover {
  color: var(--color-text);
}
.chapter-card__tab--active {
  color: var(--color-text);
  border-color: rgba(196, 181, 253, 0.6);
  background: rgba(139, 92, 246, 0.22);
  box-shadow: 0 0 8px rgba(139, 92, 246, 0.25);
}

.chapter-card__body {
  position: relative;
  padding: 4px 60px 4px 56px;
  min-height: 64px;
}
.chapter-card__quote-glyph {
  position: absolute;
  left: 6px;
  top: -6px;
  font-size: 56px;
  line-height: 1;
  font-family: 'Georgia', 'Noto Serif TC', serif;
  color: rgba(196, 181, 253, 0.45);
  pointer-events: none;
  user-select: none;
}
.chapter-card__narrative {
  margin: 0;
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
  font-size: 15px;
  line-height: 1.85;
  color: var(--color-text);
  white-space: pre-wrap;
  letter-spacing: 0.02em;
}
.chapter-card__stamp {
  position: absolute;
  right: 0;
  top: 0;
  display: inline-flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  border: 1.5px solid rgba(196, 181, 253, 0.6);
  color: rgba(196, 181, 253, 0.85);
  background: rgba(20, 16, 42, 0.6);
  transform: rotate(-8deg);
  font-family: 'Georgia', 'Noto Serif TC', serif;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  box-shadow: 0 0 14px rgba(139, 92, 246, 0.25);
}
.chapter-card__stamp-label {
  font-weight: 700;
  font-size: 12px;
}
.chapter-card__stamp-sub {
  font-size: 9px;
  opacity: 0.85;
  letter-spacing: 0.18em;
}

.chapter-card__foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 6px;
  border-top: 1px dashed rgba(139, 92, 246, 0.2);
  font-size: 11px;
  color: var(--color-text-secondary);
}
.chapter-card__period {
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.05em;
}
.chapter-card__theme {
  color: rgba(196, 181, 253, 0.85);
  letter-spacing: 0.03em;
}
.chapter-card__empty {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
</style>
