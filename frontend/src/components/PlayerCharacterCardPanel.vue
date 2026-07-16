<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import CharacterCardGalleryModal from '@/components/CharacterCardGalleryModal.vue'
import InitialRelationshipWizardModal from '@/components/InitialRelationshipWizardModal.vue'
import type { Character, InitialRelationshipPayload } from '@/types/character'
import {
  downloadCharacterCard,
  importCharacterCard,
  installCharacterCard,
  listCharacterCards,
  previewCharacterCard,
  previewCharacterCardPack,
  type CharacterCardPreview,
  type CharacterCardPackSummary,
} from '@/utils/api/characters'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  selectedCharacter: Character | null
}>()

const emit = defineEmits<{
  characterCreated: [char: Character]
}>()

const { t } = useI18n()

const packs = ref<CharacterCardPackSummary[]>([])
const loadingPacks = ref(false)
const packsError = ref<string | null>(null)
const exporting = ref(false)
const importing = ref(false)
const previewing = ref(false)
const installingId = ref<string | null>(null)
const importInputRef = ref<HTMLInputElement | null>(null)
const browseVisible = ref(false)
const browseIndex = ref(0)
const browseTranslate = ref(false)
const translatingBrowse = ref(false)
const browseTranslateError = ref<string | null>(null)
const translatedBrowseCards = ref<Record<string, CharacterCardPreview>>({})
const previewVisible = ref(false)
const originalPreviewCard = ref<CharacterCardPreview | null>(null)
const translatedPreviewCard = ref<CharacterCardPreview | null>(null)
const previewTranslate = ref(false)
const translatingPreview = ref(false)
const previewTranslateError = ref<string | null>(null)
const pendingImportFile = ref<File | null>(null)
const relationshipWizardVisible = ref(false)
const pendingRelationshipAction = ref<
  | { kind: 'upload'; card: CharacterCardPreview }
  | { kind: 'pack'; card: CharacterCardPreview }
  | null
>(null)
let previewRequestToken = 0
let browseRequestToken = 0

const browseCards = computed<CharacterCardPreview[]>(() => {
  if (!browseTranslate.value) {
    return packs.value
  }
  return packs.value.map((card) => translatedBrowseCards.value[card.pack_id] ?? card)
})

const previewCards = computed(() => {
  const card = previewTranslate.value && translatedPreviewCard.value
    ? translatedPreviewCard.value
    : originalPreviewCard.value
  return card ? [card] : []
})

const relationshipWizardCardName = computed(() => (
  pendingRelationshipAction.value?.card.name
  || pendingRelationshipAction.value?.card.title
  || ''
))

async function loadPacks() {
  loadingPacks.value = true
  packsError.value = null
  try {
    packs.value = await listCharacterCards()
  } catch (error) {
    packsError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loadingPacks.value = false
  }
}

async function openBrowse() {
  browseVisible.value = true
  browseIndex.value = 0
  browseTranslate.value = false
  translatingBrowse.value = false
  browseTranslateError.value = null
  translatedBrowseCards.value = {}
  browseRequestToken += 1
  await loadPacks()
}

async function exportSelectedCharacter() {
  if (!props.selectedCharacter) return
  exporting.value = true
  try {
    downloadCharacterCard(props.selectedCharacter.id)
    notification.success({
      message: t('playerSidebar.characterCards.exportSuccess', {
        name: props.selectedCharacter.name,
      }),
    })
  } catch (error) {
    notification.error({
      message: t('playerSidebar.characterCards.exportError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    exporting.value = false
  }
}

function triggerImport() {
  importInputRef.value?.click()
}

async function handleImportFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return

  previewing.value = true
  const requestToken = ++previewRequestToken
  try {
    pendingImportFile.value = file
    const card = await previewCharacterCard(file)
    if (requestToken !== previewRequestToken) return
    originalPreviewCard.value = card
    previewVisible.value = true
  } catch (error) {
    notification.error({
      message: t('playerSidebar.characterCards.importError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    previewing.value = false
  }
}

async function setPreviewTranslate(enabled: boolean) {
  previewTranslate.value = enabled
  previewTranslateError.value = null
  if (!enabled || translatedPreviewCard.value || !pendingImportFile.value) {
    return
  }
  const file = pendingImportFile.value
  const requestToken = ++previewRequestToken
  translatingPreview.value = true
  try {
    const card = await previewCharacterCard(file, { translate: true })
    if (requestToken !== previewRequestToken) return
    translatedPreviewCard.value = card
  } catch (error) {
    if (requestToken !== previewRequestToken) return
    previewTranslate.value = false
    previewTranslateError.value = error instanceof Error ? error.message : String(error)
    notification.error({
      message: t('playerSidebar.characterCards.translate.error'),
      description: previewTranslateError.value,
    })
  } finally {
    if (requestToken === previewRequestToken) {
      translatingPreview.value = false
    }
  }
}

async function setBrowseTranslate(enabled: boolean) {
  browseTranslate.value = enabled
  browseTranslateError.value = null
  if (enabled) {
    await ensureActiveBrowseCardTranslated()
  }
}

function changeBrowseIndex(index: number) {
  browseIndex.value = index
  if (browseTranslate.value) {
    void ensureActiveBrowseCardTranslated()
  }
}

async function ensureActiveBrowseCardTranslated() {
  const card = packs.value[browseIndex.value]
  const packId = card?.pack_id
  if (!browseTranslate.value || !packId || translatedBrowseCards.value[packId]) {
    return
  }
  const requestToken = ++browseRequestToken
  translatingBrowse.value = true
  try {
    const translated = await previewCharacterCardPack(packId, { translate: true })
    if (requestToken !== browseRequestToken) return
    translatedBrowseCards.value = {
      ...translatedBrowseCards.value,
      [packId]: translated,
    }
  } catch (error) {
    if (requestToken !== browseRequestToken) return
    browseTranslate.value = false
    browseTranslateError.value = error instanceof Error ? error.message : String(error)
    notification.error({
      message: t('playerSidebar.characterCards.translate.error'),
      description: browseTranslateError.value,
    })
  } finally {
    if (requestToken === browseRequestToken) {
      translatingBrowse.value = false
    }
  }
}

async function installPack(card: CharacterCardPreview) {
  if (!card.pack_id) return
  pendingRelationshipAction.value = { kind: 'pack', card }
  relationshipWizardVisible.value = true
}

async function runInstallPack(
  card: CharacterCardPreview,
  initialRelationship: InitialRelationshipPayload | null,
) {
  if (!card.pack_id) return
  installingId.value = card.pack_id
  try {
    const result = await installCharacterCard(
      card.pack_id,
      {
        translate: browseTranslate.value,
        initialRelationship,
      },
    )
    notifyCharacterCreated(result.character, result.landed_arc_template_ids.length)
    resetBrowseModal()
    resetRelationshipWizard()
  } catch (error) {
    notification.error({
      message: t('playerSidebar.characterCards.installError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    installingId.value = null
  }
}

function closeBrowse() {
  if (installingId.value !== null) return
  resetBrowseModal()
}

function resetBrowseModal() {
  browseRequestToken += 1
  browseVisible.value = false
  browseTranslate.value = false
  translatingBrowse.value = false
  browseTranslateError.value = null
  translatedBrowseCards.value = {}
}

async function confirmPreviewImport(_card?: CharacterCardPreview) {
  if (!pendingImportFile.value) return
  const card = _card ?? previewCards.value[0]
  if (!card) return
  pendingRelationshipAction.value = { kind: 'upload', card }
  relationshipWizardVisible.value = true
}

async function runPreviewImport(
  initialRelationship: InitialRelationshipPayload | null,
) {
  if (!pendingImportFile.value) return
  importing.value = true
  try {
    const result = await importCharacterCard(
      pendingImportFile.value,
      {
        translate: previewTranslate.value,
        initialRelationship,
      },
    )
    notifyCharacterCreated(result.character, result.landed_arc_template_ids.length)
    closePreview()
    resetRelationshipWizard()
  } catch (error) {
    notification.error({
      message: t('playerSidebar.characterCards.importError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    importing.value = false
  }
}

async function confirmRelationshipWizard(
  initialRelationship: InitialRelationshipPayload | null,
) {
  const action = pendingRelationshipAction.value
  if (!action) return
  if (action.kind === 'pack') {
    await runInstallPack(action.card, initialRelationship)
    return
  }
  await runPreviewImport(initialRelationship)
}

function closeRelationshipWizard() {
  if (importing.value || installingId.value !== null) return
  resetRelationshipWizard()
}

function resetRelationshipWizard() {
  relationshipWizardVisible.value = false
  pendingRelationshipAction.value = null
}

function closePreview() {
  if (importing.value) return
  previewRequestToken += 1
  previewVisible.value = false
  originalPreviewCard.value = null
  translatedPreviewCard.value = null
  previewTranslate.value = false
  translatingPreview.value = false
  previewTranslateError.value = null
  pendingImportFile.value = null
}

function notifyCharacterCreated(character: Character, storyCount: number) {
  notification.success({
    message: t('playerSidebar.characterCards.createdSuccess', { name: character.name }),
    description: storyCount > 0
      ? t('playerSidebar.characterCards.storySeedsAdded', { count: storyCount })
      : undefined,
  })
  emit('characterCreated', character)
}

defineExpose({
  openBrowse,
})
</script>

<template>
  <section class="character-cards">
    <p class="character-cards__hint">{{ t('playerSidebar.characterCards.hint') }}</p>
    <p class="character-cards__hint">{{ t('playerSidebar.characterCards.importHint') }}</p>

    <div class="character-cards__actions">
      <UiButton
        v-if="selectedCharacter"
        variant="secondary"
        size="sm"
        :loading="exporting"
        @click="exportSelectedCharacter"
      >
        {{ t('playerSidebar.characterCards.exportAction') }}
      </UiButton>
      <UiButton
        variant="primary"
        size="sm"
        :loading="previewing"
        @click="triggerImport"
      >
        {{ t('playerSidebar.characterCards.importAction') }}
      </UiButton>
      <UiButton
        variant="secondary"
        size="sm"
        @click="openBrowse"
      >
        {{ t('playerSidebar.characterCards.browseAction') }}
      </UiButton>
    </div>

    <input
      ref="importInputRef"
      type="file"
      accept=".lumecard,application/zip,.json,application/json,.png,image/png"
      class="character-cards__file"
      @change="handleImportFile"
    />

    <CharacterCardGalleryModal
      :visible="browseVisible"
      mode="browse"
      :cards="browseCards"
      :active-index="browseIndex"
      :loading="loadingPacks"
      :error="packsError"
      :action-loading="installingId !== null || translatingBrowse"
      :translate-enabled="browseTranslate"
      :translate-loading="translatingBrowse"
      :translate-error="browseTranslateError"
      @close="closeBrowse"
      @change="changeBrowseIndex"
      @confirm="installPack"
      @translate-change="setBrowseTranslate"
    />

    <CharacterCardGalleryModal
      :visible="previewVisible"
      mode="preview"
      :cards="previewCards"
      :action-loading="importing || translatingPreview"
      :translate-enabled="previewTranslate"
      :translate-loading="translatingPreview"
      :translate-error="previewTranslateError"
      @close="closePreview"
      @confirm="confirmPreviewImport"
      @translate-change="setPreviewTranslate"
    />
    <InitialRelationshipWizardModal
      :visible="relationshipWizardVisible"
      :card-name="relationshipWizardCardName"
      :card="pendingRelationshipAction?.card ?? null"
      :suggested-known-context="pendingRelationshipAction?.card.suggested_known_context ?? ''"
      :loading="importing || installingId !== null"
      @close="closeRelationshipWizard"
      @confirm="confirmRelationshipWizard"
    />
  </section>
</template>

<style scoped>
.character-cards {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.character-cards__hint,
.character-cards__state,
.character-cards__error {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.character-cards__error {
  color: #f4a3a3;
}

.character-cards__actions,
.character-cards__packs-head {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.character-cards__actions {
  align-items: center;
}

.character-cards__file {
  display: none;
}

.character-cards__packs {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
</style>
