<script setup lang="ts">
/**
 * 玩家導覽的一章。
 *
 * 純呈現：標題、一句定調、幾條白話、選配的一顆出口按鈕。條件化（章節級
 * hosted-only／self-host-only、句子級 `SelfHost` 兄弟鍵與整句消失）全部在
 * `PlayerGuideModal` 解完才傳進來，這裡拿到的就是這個站真的該顯示的那幾
 * 句——好讓「哪些字會出現」只有一個決定點。
 *
 * `actionLabel` 是章尾動作按鈕的通用槽：有值才畫出那顆按鈕，按下去要做什
 * 麼由上層決定（目前唯一的用途是創作專區那一章——它不重寫創作區的內容，
 * 只把既有的「創作區怎麼玩」導覽叫出來），這裡只負責畫出來並把事件往上
 * 丟。
 */
import { UiButton } from '@/components/ui'

defineProps<{
  icon: string
  title: string
  lead: string
  items: readonly string[]
  headingId: string
  actionLabel?: string
}>()

defineEmits<{ action: [] }>()
</script>

<template>
  <section class="guide-chapter" :aria-labelledby="headingId">
    <header class="guide-chapter__head">
      <span class="guide-chapter__icon" aria-hidden="true">{{ icon }}</span>
      <h3 :id="headingId">{{ title }}</h3>
    </header>
    <p class="guide-chapter__lead">{{ lead }}</p>
    <ul class="guide-chapter__items">
      <li v-for="(item, index) in items" :key="index">{{ item }}</li>
    </ul>
    <div v-if="actionLabel" class="guide-chapter__action">
      <UiButton variant="secondary" size="sm" @click="$emit('action')">
        {{ actionLabel }}
      </UiButton>
    </div>
  </section>
</template>

<style scoped>
.guide-chapter {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  scroll-margin-top: var(--space-2);
}

.guide-chapter__head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.guide-chapter__icon {
  width: 26px;
  height: 26px;
  flex: 0 0 auto;
  border-radius: 999px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  line-height: 1;
  color: var(--color-spark);
  background: rgba(var(--color-primary-rgb), 0.14);
}

.guide-chapter__head h3 {
  margin: 0;
  min-width: 0;
  font-size: var(--font-lg);
  font-weight: 650;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}

.guide-chapter__lead {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.7;
}

.guide-chapter__items {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 8px;
}

/* 每條用左側短線標出「這是一條」，不另外畫圖示——九章下來圖示只會變雜訊。 */
.guide-chapter__items li {
  position: relative;
  padding: 8px 12px 8px 22px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--color-text-secondary);
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.guide-chapter__action {
  display: flex;
}

.guide-chapter__items li::before {
  content: "";
  position: absolute;
  left: 8px;
  top: 17px;
  width: 8px;
  height: 2px;
  border-radius: 2px;
  background: rgba(var(--color-primary-rgb), 0.7);
}
</style>
