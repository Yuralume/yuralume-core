<script setup lang="ts">
/**
 * 創作區分頁卡（BD12）。
 *
 * 卡片本體仍是一條 RouterLink——導覽語意不變；三段式說明放在連結
 * *外面* 的 `<details>` 裡。這個切法不是排版偏好：把可展開的按鈕塞進
 * `<a>` 會生出巢狀互動元素（無效 HTML、鍵盤與螢幕閱讀器都會亂），而
 * hover 展開在觸控裝置上等於看不到。原生 `<details>` 零 JS、鍵盤可用、
 * 手機可點，展開狀態也不需要任何元件狀態。
 */
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  routeName: string
  label: string
  /** 卡片上那一行短提示（維持原本掃視性）。 */
  description: string
  icon: string
  accent: string
  /** 三段式說明：能幹嘛／怎麼玩／成果可以轉去哪。 */
  what: string
  how: string
  next: string
  active: boolean
}>()

const { t } = useI18n()
</script>

<template>
  <div class="studio-tab-card" :style="{ '--studio-tab-accent': props.accent }">
    <RouterLink
      class="studio-tab sheen-hover"
      :class="{ 'studio-tab--active': props.active }"
      :to="{ name: props.routeName }"
    >
      <span class="studio-tab__icon" aria-hidden="true">{{ props.icon }}</span>
      <span class="studio-tab__label">{{ props.label }}</span>
      <small>{{ props.description }}</small>
    </RouterLink>

    <details class="studio-tab-card__more">
      <summary>{{ t('studio.tabs.detailsLabel') }}</summary>
      <dl class="studio-tab-card__facets">
        <dt>{{ t('studio.tabs.detailsWhat') }}</dt>
        <dd>{{ props.what }}</dd>
        <dt>{{ t('studio.tabs.detailsHow') }}</dt>
        <dd>{{ props.how }}</dd>
        <dt>{{ t('studio.tabs.detailsNext') }}</dt>
        <dd>{{ props.next }}</dd>
      </dl>
    </details>
  </div>
</template>

<style scoped>
.studio-tab-card {
  --studio-tab-accent: var(--color-primary);
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.studio-tab {
  min-width: 0;
  min-height: 92px;
  padding: var(--space-3);
  border: 1px solid color-mix(in srgb, var(--studio-tab-accent) 32%, transparent);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.07), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.58);
  color: var(--color-text);
  text-decoration: none;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  grid-template-rows: auto 1fr;
  align-items: center;
  justify-content: center;
  column-gap: var(--space-2);
  row-gap: 4px;
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.025) inset;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
}

.studio-tab:hover {
  transform: translateY(-2px);
  border-color: color-mix(in srgb, var(--studio-tab-accent) 68%, transparent);
  box-shadow:
    0 12px 28px rgba(0, 0, 0, 0.22),
    0 0 22px color-mix(in srgb, var(--studio-tab-accent) 20%, transparent);
}

.studio-tab--active {
  border-color: color-mix(in srgb, var(--studio-tab-accent) 82%, transparent);
  background:
    linear-gradient(145deg, color-mix(in srgb, var(--studio-tab-accent) 22%, transparent), rgba(255, 255, 255, 0.03)),
    rgba(18, 12, 42, 0.66);
  box-shadow:
    0 0 0 1px color-mix(in srgb, var(--studio-tab-accent) 28%, transparent) inset,
    0 0 26px color-mix(in srgb, var(--studio-tab-accent) 20%, transparent);
}

.studio-tab__icon {
  width: 30px;
  height: 30px;
  border-radius: 999px;
  color: var(--studio-tab-accent);
  background: color-mix(in srgb, var(--studio-tab-accent) 14%, transparent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  line-height: 1;
}

.studio-tab__label {
  font-weight: 650;
  overflow-wrap: anywhere;
}

.studio-tab small {
  grid-column: 2;
  color: var(--color-text-secondary);
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.studio-tab-card__more {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.025);
}

.studio-tab-card__more > summary {
  padding: 6px var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  cursor: pointer;
  list-style-position: inside;
}

.studio-tab-card__more > summary:hover {
  color: var(--color-text);
}

.studio-tab-card__more[open] > summary {
  border-bottom: 1px solid var(--color-border);
}

.studio-tab-card__facets {
  margin: 0;
  padding: var(--space-3);
  display: grid;
  gap: 2px;
}

.studio-tab-card__facets dt {
  color: var(--color-spark);
  font-size: var(--font-xs);
  font-weight: 700;
  letter-spacing: 0.04em;
}

.studio-tab-card__facets dt:not(:first-of-type) {
  margin-top: var(--space-2);
}

.studio-tab-card__facets dd {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-md);
  line-height: 1.65;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .studio-tab,
  .studio-tab:hover {
    transform: none;
    transition: none;
  }
}
</style>
