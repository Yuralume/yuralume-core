<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import type {
  CharacterVisualGenerationStyle,
  VisualSubjectType,
} from '@/types/character'

const genderIdentity = defineModel<string>('genderIdentity', { default: '' })
const thirdPersonPronoun = defineModel<string>('thirdPersonPronoun', { default: '' })
const visualGenderPresentation = defineModel<string>('visualGenderPresentation', { default: '' })
const visualSubjectType = defineModel<VisualSubjectType>('visualSubjectType', { default: 'auto' })
const visualGenerationStyle = defineModel<CharacterVisualGenerationStyle>('visualGenerationStyle', { default: '' })

const { t } = useI18n()

const visualSubjectOptions = [
  'auto',
  'human',
  'animal',
  'anthropomorphic',
  'creature',
  'object',
] as const

const visualGenerationStyleOptions = [
  '',
  'anime',
  'realistic',
] as const
</script>

<template>
  <div class="character-identity-fields">
    <label class="field-label">{{ t('characterCreate.fields.genderIdentity.label') }}</label>
    <input
      v-model="genderIdentity"
      class="field-input"
      :placeholder="t('characterCreate.fields.genderIdentity.placeholder')"
    />
    <div class="field-hint">
      {{ t('characterCreate.fields.genderIdentity.hint') }}
    </div>

    <label class="field-label">{{ t('characterCreate.fields.thirdPersonPronoun.label') }}</label>
    <input
      v-model="thirdPersonPronoun"
      class="field-input"
      :placeholder="t('characterCreate.fields.thirdPersonPronoun.placeholder')"
    />
    <div class="field-hint">
      {{ t('characterCreate.fields.thirdPersonPronoun.hint') }}
    </div>

    <label class="field-label">{{ t('characterCreate.fields.visualGenderPresentation.label') }}</label>
    <input
      v-model="visualGenderPresentation"
      class="field-input"
      :placeholder="t('characterCreate.fields.visualGenderPresentation.placeholder')"
    />
    <div class="field-hint">
      {{ t('characterCreate.fields.visualGenderPresentation.hint') }}
    </div>

    <label class="field-label">{{ t('characterCreate.fields.visualSubjectType.label') }}</label>
    <select v-model="visualSubjectType" class="field-select">
      <option
        v-for="option in visualSubjectOptions"
        :key="option"
        :value="option"
      >
        {{ t(`characterCreate.fields.visualSubjectType.options.${option}`) }}
      </option>
    </select>
    <div class="field-hint">
      {{ t('characterCreate.fields.visualSubjectType.hint') }}
    </div>

    <label class="field-label">{{ t('characterCreate.fields.visualGenerationStyle.label') }}</label>
    <select v-model="visualGenerationStyle" class="field-select">
      <option
        v-for="option in visualGenerationStyleOptions"
        :key="option || 'inherit'"
        :value="option"
      >
        {{ t(`characterCreate.fields.visualGenerationStyle.options.${option || 'inherit'}`) }}
      </option>
    </select>
    <div class="field-hint">
      {{ t('characterCreate.fields.visualGenerationStyle.hint') }}
    </div>
  </div>
</template>

<style scoped>
.character-identity-fields {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
</style>
