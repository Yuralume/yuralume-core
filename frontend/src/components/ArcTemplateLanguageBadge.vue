<script setup lang="ts">
/**
 * Source-language badge for an arc template.
 *
 * Extracted from `ArcTemplatePicker.vue` (plan A1) so the picker and the
 * Creator Studio list render the same badge from one place — the next
 * localisation-surface tweak (e.g. a "machine-translated" hint) changes
 * one file, not two (plan §remaining-fork note).
 *
 * Pass the `language` of the *displayed* view: when the translate toggle
 * is on, that is the translated view's language, so the badge tracks what
 * the operator is actually reading.
 */
import { useI18n } from 'vue-i18n'

defineProps<{
  language: string
}>()

const { t } = useI18n()
</script>

<template>
  <span
    class="pill pill-lang"
    :title="t('story.arcTemplatePicker.languageBadgeTitle', { language })"
  >{{ language }}</span>
</template>

<style scoped>
.pill {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  font-weight: 600;
}

.pill-lang {
  background: rgba(var(--color-text-rgb, 226, 232, 240), 0.1);
  color: var(--color-text-muted, #94a3b8);
  border: 1px solid rgba(var(--color-text-rgb, 226, 232, 240), 0.18);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
</style>
