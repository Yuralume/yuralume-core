<script setup lang="ts">
/**
 * /admin/characters — 角色卡（A 層靜態設定）單一編輯入口。
 *
 * - 頂部：新增角色 / 匯入角色卡（.lumecard）按鈕
 * - 每位角色：AdminCharacterPicker 選角 → 掛 CharacterCardEditor，把原本
 *   散落在 /admin/world、/admin/dispositions、/admin/proactive、story 區與
 *   舞台圖的 A 層設定收斂成四個折疊段（身分人格 / 世界觀行為 / 主線劇情 /
 *   舞台圖），並提供「匯出角色卡」。
 *
 * B 層（voice / loras / 路由 profile）是部署綁定、不隨卡走，維持在各自
 * admin page；那些舊子頁仍鏡像保留，角色卡只是把同一套 inner editor 收斂
 * 到一處呈現。
 */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import type { Character } from '@/types/character'
import CharacterCardEditor from '@/components/admin/CharacterCardEditor.vue'
import CharacterCardMarketplace from '@/components/admin/CharacterCardMarketplace.vue'
import CharacterCreateModal from '@/components/CharacterCreateModal.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiCard, UiBadge, UiButton } from '@/components/ui'
import { downloadCharacterCard, importCharacterCard } from '@/utils/api/characters'

const { t } = useI18n()

const pickerRef = ref<InstanceType<typeof AdminCharacterPicker> | null>(null)
const createModalOpen = ref(false)
const exportingId = ref<string | null>(null)
const importFileRef = ref<HTMLInputElement | null>(null)
const importing = ref(false)

async function handleExportCard(character: Character) {
  exportingId.value = character.id
  try {
    downloadCharacterCard(character.id)
    notification.success({
      message: t('admin.page.characters.exportCardSuccess', { name: character.name }),
    })
  } catch (error) {
    notification.error({
      message: t('admin.page.characters.exportCardError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    exportingId.value = null
  }
}

function triggerImport() {
  importFileRef.value?.click()
}

async function handleImportFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  // Reset immediately so picking the same file twice still fires change.
  input.value = ''
  if (!file) return

  importing.value = true
  try {
    const { character, landed_arc_template_ids } = await importCharacterCard(file)
    await pickerRef.value?.refresh()
    notification.success({
      message: t('admin.page.characters.importCardSuccess', { name: character.name }),
      description: landed_arc_template_ids.length
        ? t('admin.page.characters.importCardTemplates', {
            count: landed_arc_template_ids.length,
          })
        : undefined,
    })
  } catch (error) {
    notification.error({
      message: t('admin.page.characters.importCardError'),
      description: error instanceof Error ? error.message : String(error),
    })
  } finally {
    importing.value = false
  }
}

function openCreateModal() {
  createModalOpen.value = true
}

function closeCreateModal() {
  createModalOpen.value = false
}

async function handleCharacterCreated(_char: Character) {
  createModalOpen.value = false
  // 讓 picker 重撈清單，這樣新建的角色會出現在下拉裡
  await pickerRef.value?.refresh()
}

async function handleCardInstalled(_char: Character) {
  // 市集安裝完一張卡 = 新增了一個角色，刷新 picker 讓它出現在下拉裡
  await pickerRef.value?.refresh()
}

function handleUpdated(updated: Character) {
  // Picker 內部維護的 list 同步 — 避免下拉清單還停留在舊名字 / 舊頭像
  pickerRef.value?.patch(updated)
}

function handleDataReset(_char: Character) {
  // 清空記憶 / 對話不會改動 Character 本身，所以這裡不需要 patch picker
  // list；保留 hook 以便未來想刷新 status badge 之類的時候可以接。
}
</script>

<template>
  <div class="characters-admin">
    <header class="characters-admin__header">
      <div>
        <h1>{{ t('admin.page.characters.title') }}</h1>
        <p class="characters-admin__subtitle">
          {{ t('admin.page.characters.subtitlePrefix') }}
          <strong>{{ t('admin.page.characters.subtitleStrong') }}</strong>
          {{ t('admin.page.characters.subtitleSuffix') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <UiCard>
      <template #header>
        <h2 class="characters-admin__card-title">{{ t('admin.page.characters.createTitle') }}</h2>
      </template>
      <p class="characters-admin__create-hint">
        {{ t('admin.page.characters.createHint') }}
      </p>
      <div class="characters-admin__create-actions">
        <UiButton variant="primary" @click="openCreateModal">
          {{ t('admin.page.characters.createAction') }}
        </UiButton>
        <UiButton variant="secondary" :loading="importing" @click="triggerImport">
          {{ t('admin.page.characters.importCardAction') }}
        </UiButton>
      </div>
      <p class="characters-admin__import-hint">
        {{ t('admin.page.characters.importCardHint') }}
      </p>
      <input
        ref="importFileRef"
        type="file"
        accept=".lumecard,application/zip,.json,application/json,.png,image/png"
        class="characters-admin__import-input"
        @change="handleImportFile"
      />
    </UiCard>

    <CharacterCardMarketplace @installed="handleCardInstalled" />

    <AdminCharacterPicker
      ref="pickerRef"
      :title="t('admin.page.characters.pickerTitle')"
      :hint="t('admin.page.characters.pickerHint')"
    >
      <template #default="{ character }">
        <UiCard size="lg">
          <template #header>
            <h2 class="characters-admin__card-title">{{ character.name }}</h2>
          </template>
          <template #actions>
            <UiButton
              variant="secondary"
              size="sm"
              :loading="exportingId === character.id"
              @click="handleExportCard(character)"
            >
              {{ t('admin.page.characters.exportCardAction') }}
            </UiButton>
          </template>
          <CharacterCardEditor
            :key="character.id"
            :character="character"
            :show-tool-settings="true"
            @updated="handleUpdated"
            @data-reset="handleDataReset"
          />
        </UiCard>
      </template>
    </AdminCharacterPicker>

    <CharacterCreateModal
      v-if="createModalOpen"
      @close="closeCreateModal"
      @created="handleCharacterCreated"
    />
  </div>
</template>

<style scoped>
.characters-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.characters-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.characters-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.characters-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.characters-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.characters-admin__create-hint {
  margin: 0 0 var(--space-3);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.characters-admin__create-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-self: flex-start;
}
.characters-admin__import-hint {
  margin: var(--space-2) 0 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.characters-admin__import-input {
  display: none;
}
</style>
