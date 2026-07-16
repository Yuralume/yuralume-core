<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import PendingFollowUpsPanel from '@/components/PendingFollowUpsPanel.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiBadge } from '@/components/ui'

const { t } = useI18n()

/**
 * PendingFollowUpsPanel already self-loads on ``watch(() => props.characterId)``,
 * so no inner-editor wrapper needed — feed the picked character.id through.
 *
 * Two kinds live in this panel:
 *  - busy-defer pending replies (delayed because character was occupied)
 *  - scheduled promises (LLM-set future "I'll message you at X" commitments)
 * Both are read+act here; the player sidebar still shows a read-only hint
 * card so users see incoming activity without reaching for admin.
 */
</script>

<template>
  <div class="follow-ups-admin">
    <header class="follow-ups-admin__header">
      <div>
        <h1>{{ t('admin.page.followUps.title') }}</h1>
        <p class="follow-ups-admin__subtitle">
          {{ t('admin.page.followUps.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.followUps.pickerHint')"
    >
      <template #default="{ character }">
        <PendingFollowUpsPanel :character-id="character.id" />
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.follow-ups-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.follow-ups-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.follow-ups-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.follow-ups-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
