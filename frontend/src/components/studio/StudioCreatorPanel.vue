<script setup lang="ts">
defineProps<{
  eyebrow: string
  title: string
  notice?: string
}>()
</script>

<template>
  <section class="studio-creator-panel glass-panel sheen-hover">
    <div class="studio-creator-panel__heading">
      <p class="spark-label">{{ eyebrow }}</p>
      <h2 class="display-title">{{ title }}</h2>
    </div>

    <div class="studio-creator-panel__body">
      <slot />
    </div>

    <p v-if="notice" class="studio-creator-panel__notice">
      {{ notice }}
    </p>
  </section>
</template>

<style scoped>
.studio-creator-panel {
  width: min(100%, 640px);
  margin: 0 auto;
  padding: var(--space-5);
  border-radius: 8px;
  background:
    linear-gradient(rgba(18, 12, 42, 0.82), rgba(18, 12, 42, 0.82)) padding-box,
    linear-gradient(
      135deg,
      rgba(var(--color-spark-rgb), 0.42),
      rgba(var(--color-primary-rgb), 0.72),
      rgba(var(--color-secondary-rgb), 0.34)
    ) border-box;
  border: 1px solid transparent;
  display: grid;
  /* 顯式 minmax(0, 1fr)：隱式 auto 欄會被子內容的 min-content 撐寬，
     一旦 slot 內容含不可斷行元素，整個面板會超出手機畫面。 */
  grid-template-columns: minmax(0, 1fr);
  gap: var(--space-4);
}

.studio-creator-panel__heading {
  display: grid;
  gap: var(--space-1);
}

.studio-creator-panel__heading h2,
.studio-creator-panel__heading p,
.studio-creator-panel__notice {
  margin: 0;
}

.studio-creator-panel__heading h2 {
  font-size: 30px;
  color: var(--color-text);
}

.studio-creator-panel__body {
  display: grid;
  gap: var(--space-3);
}

.studio-creator-panel__notice {
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
}

@media (max-width: 768px) {
  .studio-creator-panel {
    padding: var(--space-4);
  }

  .studio-creator-panel__heading h2 {
    font-size: 24px;
  }
}
</style>
