<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character, ScheduleInvolvementPolicy } from '@/types/character'
import { useInitialRelationshipSettings } from '@/composables/useInitialRelationshipSettings'
import { UiButton, UiInput, UiSelect, UiTextarea } from '@/components/ui'
import type { UiSelectOption } from '@/components/ui'

/**
 * 設定頁「關係與相處設定」——建立角色時填的整份初始關係 seed，創完之後
 * 一樣可以再調整（IR2）。互動比照建立角色時的
 * `InitialRelationshipWizardModal`：行程參與程度是四選一下拉，勾了主動
 * 訊息授權才展開頻率提示，其餘是純文字欄。
 *
 * 兩個稱呼欄位（原本獨立的 `RelationshipNamesEditor`）併進同一份表單、
 * 同一次 PATCH：後端 `update_seed` 內部會轉呼 `update_names`，rename-log
 * 與已學到的人設投影一樣會同步，不必也不該在前端另外呼叫
 * `/relationship-names`。
 */
const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()
const { form, loading, loaded, saving, hasChanges, load, save } = useInitialRelationshipSettings()

const feedback = ref<string | null>(null)
const feedbackIsError = ref(false)

/** 讀不到現況：既不是「載入中」也不是「還沒填」，不能拿其中任一個充數。 */
const loadFailed = computed(() => !loading.value && !loaded.value)

const scheduleOptions = computed<UiSelectOption[]>(() => ([
  { value: 'none', label: t('characterCreate.initialRelationship.scheduleOptions.none') },
  { value: 'mention_only', label: t('characterCreate.initialRelationship.scheduleOptions.mentionOnly') },
  { value: 'invite_required', label: t('characterCreate.initialRelationship.scheduleOptions.inviteRequired') },
  { value: 'shared_allowed', label: t('characterCreate.initialRelationship.scheduleOptions.sharedAllowed') },
]))

function setSchedulePolicy(value: string) {
  form.value.schedule_involvement_policy = value as ScheduleInvolvementPolicy
}

watch(() => props.character.id, (characterId) => {
  feedback.value = null
  void load(characterId)
}, { immediate: true })

async function handleSave() {
  feedback.value = null
  feedbackIsError.value = false
  try {
    const result = await save()
    if (result.changed) {
      feedback.value = t('playerSidebar.relationshipSeed.saved')
    }
  } catch (err) {
    feedbackIsError.value = true
    feedback.value = err instanceof Error
      ? t('common.errorWithDetail', {
        message: t('playerSidebar.relationshipSeed.saveFailed'),
        detail: err.message,
      })
      : t('playerSidebar.relationshipSeed.saveFailed')
  }
}
</script>

<template>
  <div class="relationship-seed">
    <p class="relationship-seed__hint">
      {{ t('playerSidebar.relationshipSeed.sectionHint') }}
    </p>

    <p v-if="loading" class="relationship-seed__hint">
      {{ t('playerSidebar.relationshipSeed.loading') }}
    </p>
    <p v-else-if="loadFailed" class="relationship-seed__failed" role="alert">
      {{ t('playerSidebar.relationshipSeed.loadFailed') }}
    </p>

    <template v-else>
      <UiInput
        v-model="form.relationship_label"
        :label="t('characterCreate.initialRelationship.relationshipLabel')"
        :placeholder="t('characterCreate.initialRelationship.relationshipPlaceholder')"
        :disabled="saving"
      />

      <div class="relationship-seed__grid">
        <UiInput
          v-model="form.user_address_name"
          :label="t('characterCreate.initialRelationship.userAddress')"
          :placeholder="t('characterCreate.initialRelationship.userAddressPlaceholder')"
          :disabled="saving"
        />
        <UiInput
          v-model="form.character_address_name"
          :label="t('characterCreate.initialRelationship.characterAddress')"
          :placeholder="t('characterCreate.initialRelationship.characterAddressPlaceholder')"
          :disabled="saving"
        />
      </div>
      <p class="relationship-seed__hint">{{ t('playerSidebar.relationshipSeed.namesHint') }}</p>
      <p class="relationship-seed__hint">
        {{ t('playerSidebar.relationshipSeed.namesReconcileHint', { name: character.name }) }}
      </p>

      <UiTextarea
        v-model="form.known_context"
        :label="t('characterCreate.initialRelationship.knownContext')"
        :placeholder="t('playerSidebar.characterCards.relationship.knownContextPlaceholder')"
        :rows="2"
        :disabled="saving"
      />

      <UiInput
        v-model="form.living_arrangement"
        :label="t('characterCreate.initialRelationship.livingArrangement')"
        :placeholder="t('characterCreate.initialRelationship.livingPlaceholder')"
        :disabled="saving"
      />

      <UiInput
        v-model="form.tone_distance"
        :label="t('characterCreate.initialRelationship.toneDistance')"
        :placeholder="t('characterCreate.initialRelationship.toneDistancePlaceholder')"
        :disabled="saving"
      />

      <UiInput
        v-model="form.familiarity_boundary"
        :label="t('characterCreate.initialRelationship.boundary')"
        :placeholder="t('playerSidebar.characterCards.relationship.boundaryPlaceholder')"
        :disabled="saving"
      />

      <UiSelect
        :model-value="form.schedule_involvement_policy"
        :label="t('characterCreate.initialRelationship.scheduleLabel')"
        :options="scheduleOptions"
        :disabled="saving"
        @update:model-value="setSchedulePolicy"
      />

      <label class="relationship-seed__checkbox">
        <input v-model="form.proactive_permission" type="checkbox" :disabled="saving" />
        <span>{{ t('characterCreate.initialRelationship.proactivePermission') }}</span>
      </label>
      <UiInput
        v-if="form.proactive_permission"
        v-model="form.proactive_cadence_hint"
        :placeholder="t('characterCreate.initialRelationship.proactiveCadencePlaceholder')"
        :hint="t('characterCreate.initialRelationship.proactiveCadenceHint')"
        :disabled="saving"
      />

      <UiTextarea
        v-model="form.user_profile_notes"
        :label="t('playerSidebar.relationshipSeed.notesLabel')"
        :placeholder="t('characterCreate.initialRelationship.notesPlaceholder')"
        :rows="2"
        :disabled="saving"
      />

      <div class="relationship-seed__actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="saving"
          :disabled="!hasChanges"
          @click="handleSave"
        >
          {{ saving ? t('playerSidebar.relationshipSeed.saving') : t('playerSidebar.relationshipSeed.save') }}
        </UiButton>
      </div>
      <p
        v-if="feedback"
        class="relationship-seed__feedback"
        :class="{ 'relationship-seed__feedback--error': feedbackIsError }"
      >
        {{ feedback }}
      </p>
    </template>
  </div>
</template>

<style scoped>
.relationship-seed {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.relationship-seed__grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.relationship-seed__checkbox {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  color: var(--color-text);
  font-size: var(--font-xs);
  line-height: 1.5;
  cursor: pointer;
}

.relationship-seed__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.relationship-seed__hint,
.relationship-seed__feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.relationship-seed__failed {
  margin: 0;
  color: #f4a3a3;
  font-size: 11px;
  line-height: 1.45;
}

.relationship-seed__feedback {
  color: #7dc49a;
}

.relationship-seed__feedback--error {
  color: #f4a3a3;
}

@media (max-width: 480px) {
  .relationship-seed__grid {
    grid-template-columns: 1fr;
  }
}
</style>
