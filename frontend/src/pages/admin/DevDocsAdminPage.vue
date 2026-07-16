<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiCard } from '@/components/ui'
import { DEV_DOCS, resolveDevDoc } from '@/utils/devDocs'
import { renderDevDocMarkdown } from '@/utils/devDocsMarkdown'

// Admin → Developer Docs (D9/D10, CUSTOM_MEDIA_GATEWAY_SPEC_AND_COMFYUI_PLAN.md):
// a small registry-backed reader for repo-shipped developer specs. Doc list
// UI chrome is i18n; the markdown body stays English (D6) — it is developer
// -facing spec content, not app UI copy.

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const activeDoc = computed(() => resolveDevDoc(route.params.slug as string | undefined))
const activeHtml = computed(() => (activeDoc.value ? renderDevDocMarkdown(activeDoc.value.source) : ''))

function selectDoc(slug: string): void {
  router.push({ name: 'admin-dev-docs-detail', params: { slug } })
}
</script>

<template>
  <div class="dev-docs">
    <header class="dev-docs__header">
      <div>
        <h1>{{ t('admin.devDocs.title') }}</h1>
        <p class="dev-docs__subtitle">{{ t('admin.devDocs.subtitle') }}</p>
      </div>
      <UiBadge variant="primary">{{ t('admin.devDocs.badge') }}</UiBadge>
    </header>

    <div class="dev-docs__grid">
      <nav class="dev-docs__list" aria-label="Developer docs">
        <RouterLink
          v-for="doc in DEV_DOCS"
          :key="doc.slug"
          :to="{ name: 'admin-dev-docs-detail', params: { slug: doc.slug } }"
          class="dev-docs__list-item"
          :class="{ 'is-active': activeDoc?.slug === doc.slug }"
          @click.prevent="selectDoc(doc.slug)"
        >
          {{ t(doc.titleKey) }}
        </RouterLink>
      </nav>

      <UiCard v-if="activeDoc" size="lg" class="dev-docs__reader">
        <template #header>
          <div>
            <h2 class="dev-docs__doc-title">{{ t(activeDoc.titleKey) }}</h2>
            <p class="dev-docs__doc-hint">{{ t('admin.devDocs.englishContentHint') }}</p>
          </div>
        </template>
        <!-- eslint-disable-next-line vue/no-v-html -- rendered from our own shipped repo markdown, not user input; markdown-it html:false blocks raw HTML passthrough (see devDocsMarkdown.ts) -->
        <div class="dev-docs__body" v-html="activeHtml" />
      </UiCard>
      <p v-else class="dev-docs__empty">{{ t('admin.devDocs.empty') }}</p>
    </div>
  </div>
</template>

<style scoped>
.dev-docs {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.dev-docs__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.dev-docs__subtitle {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  max-width: 60ch;
}

.dev-docs__grid {
  display: grid;
  grid-template-columns: 220px 1fr;
  gap: var(--space-5);
  align-items: start;
}

.dev-docs__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  position: sticky;
  top: 0;
}

.dev-docs__list-item {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md, 8px);
  color: var(--color-text-secondary);
  text-decoration: none;
  font-size: var(--font-sm);
  border-left: 2px solid transparent;
}
.dev-docs__list-item:hover {
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
}
.dev-docs__list-item.is-active {
  background: rgba(232, 155, 133, 0.10);
  border-left-color: var(--color-primary);
  color: var(--color-primary);
}

.dev-docs__doc-title {
  margin: 0;
  font-size: var(--font-lg);
}
.dev-docs__doc-hint {
  margin: var(--space-1) 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
}

.dev-docs__empty {
  color: var(--color-text-secondary);
}

/* Rendered markdown body: scoped layout only (spacing/scroll), no color or
   font overrides here — those come from the shared markdown typography
   below so headings/code/links stay legible on the dark theme without
   duplicating base visual styles per CLAUDE.md's global-form-style rule. */
.dev-docs__body {
  max-width: 78ch;
  overflow-x: auto;
}
.dev-docs__body :deep(h1),
.dev-docs__body :deep(h2),
.dev-docs__body :deep(h3) {
  color: var(--color-text);
  margin-top: var(--space-5);
  margin-bottom: var(--space-2);
}
.dev-docs__body :deep(h1:first-child),
.dev-docs__body :deep(h2:first-child) {
  margin-top: 0;
}
.dev-docs__body :deep(p),
.dev-docs__body :deep(li) {
  color: var(--color-text);
  line-height: 1.6;
}
.dev-docs__body :deep(code) {
  background: rgba(255, 255, 255, 0.08);
  padding: 0 4px;
  border-radius: 4px;
  font-size: 0.9em;
}
.dev-docs__body :deep(pre) {
  background: rgba(0, 0, 0, 0.35);
  padding: var(--space-3);
  border-radius: var(--radius-md, 8px);
  overflow-x: auto;
}
.dev-docs__body :deep(pre code) {
  background: none;
  padding: 0;
}
.dev-docs__body :deep(blockquote) {
  border-left: 3px solid var(--color-border);
  margin: var(--space-3) 0;
  padding: 0 var(--space-3);
  color: var(--color-text-secondary);
}
.dev-docs__body :deep(a) {
  color: var(--color-primary);
}
.dev-docs__body :deep(table) {
  border-collapse: collapse;
  width: 100%;
}
.dev-docs__body :deep(th),
.dev-docs__body :deep(td) {
  border: 1px solid var(--color-border);
  padding: var(--space-1) var(--space-2);
  text-align: left;
}

@media (max-width: 700px) {
  .dev-docs__grid {
    grid-template-columns: 1fr;
  }
  .dev-docs__list {
    position: static;
    flex-direction: row;
    flex-wrap: wrap;
  }
}
</style>
