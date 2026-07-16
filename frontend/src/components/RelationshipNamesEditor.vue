<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import {
  getRelationshipNames,
  updateRelationshipNames,
} from '@/utils/api/relationshipNames'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()

const userAddressName = ref('')
const characterAddressName = ref('')
const loading = ref(false)
const saving = ref(false)
const feedback = ref<string | null>(null)

async function load() {
  loading.value = true
  feedback.value = null
  try {
    const names = await getRelationshipNames(props.character.id)
    userAddressName.value = names.user_address_name
    characterAddressName.value = names.character_address_name
  } catch (err) {
    feedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.relationshipNames.loadFailed'), detail: err.message })
      : t('playerSidebar.relationshipNames.loadFailed')
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  feedback.value = null
  try {
    const names = await updateRelationshipNames(props.character.id, {
      user_address_name: userAddressName.value.trim(),
      character_address_name: characterAddressName.value.trim(),
    })
    userAddressName.value = names.user_address_name
    characterAddressName.value = names.character_address_name
    feedback.value = t('playerSidebar.relationshipNames.saved')
  } catch (err) {
    feedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.relationshipNames.saveFailed'), detail: err.message })
      : t('playerSidebar.relationshipNames.saveFailed')
  } finally {
    saving.value = false
  }
}

watch(() => props.character.id, () => { void load() }, { immediate: true })
</script>

<template>
  <div class="relationship-names">
    <p class="relationship-names__hint">
      {{ t('playerSidebar.relationshipNames.sectionHint') }}
    </p>
    <div class="relationship-names__field">
      <label class="field-label" :for="`rel-user-${character.id}`">
        {{ t('playerSidebar.relationshipNames.userAddressLabel', { name: character.name }) }}
      </label>
      <input
        :id="`rel-user-${character.id}`"
        v-model="userAddressName"
        type="text"
        class="field-input"
        maxlength="80"
        :placeholder="t('playerSidebar.relationshipNames.userAddressPlaceholder')"
        :disabled="loading || saving"
      />
    </div>
    <div class="relationship-names__field">
      <label class="field-label" :for="`rel-char-${character.id}`">
        {{ t('playerSidebar.relationshipNames.characterAddressLabel', { name: character.name }) }}
      </label>
      <input
        :id="`rel-char-${character.id}`"
        v-model="characterAddressName"
        type="text"
        class="field-input"
        maxlength="80"
        :placeholder="t('playerSidebar.relationshipNames.characterAddressPlaceholder')"
        :disabled="loading || saving"
      />
    </div>
    <div class="relationship-names__actions">
      <UiButton
        variant="primary"
        size="sm"
        :loading="saving"
        :disabled="loading"
        @click="save"
      >
        {{ saving ? t('playerSidebar.relationshipNames.saving') : t('playerSidebar.relationshipNames.save') }}
      </UiButton>
    </div>
    <p class="relationship-names__hint">
      {{ t('playerSidebar.relationshipNames.hint') }}
    </p>
    <p class="relationship-names__hint">
      {{ t('playerSidebar.relationshipNames.reconcileHint', { name: character.name }) }}
    </p>
    <p v-if="feedback" class="relationship-names__feedback">{{ feedback }}</p>
  </div>
</template>

<style scoped>
.relationship-names {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.relationship-names__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.relationship-names__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.relationship-names__hint,
.relationship-names__feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.relationship-names__feedback {
  color: #7dc49a;
}
</style>
