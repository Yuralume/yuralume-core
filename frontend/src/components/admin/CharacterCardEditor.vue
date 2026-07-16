<script setup lang="ts">
/**
 * 角色卡編輯區（M3）—— 把散落在多個 admin 子頁的「A 層靜態角色設定」
 * 收斂成單一處，分四段折疊呈現：
 *
 *   1. 身分與人格 —— CharacterEditPanel（人設核心 / 外觀 / 生日 /
 *      companions / 工具 / 漂移急救包）+ DispositionAdminEditor（內在動機四維）
 *   2. 世界觀與行為 —— WorldAdminEditor（world_frame / RSS 訂閱 / 話題過濾）
 *      + ProactiveAdminEditor（proactive / feed 節奏）
 *   3. 主線劇情 —— StoryArcPanel 的 arc-template 綁定（從 story 區「提到」
 *      角色卡層級，避免使用者忘記某角色有主線）
 *   4. 舞台圖 —— CharacterImagesPanel（image_urls，A 層、會被打包進 .lumecard）
 *
 * 設計約束（呼應 docs/CHARACTER_CARD_PLAN.md §5 與 FRONTEND_REFACTOR_PLAN.md）：
 * 本元件由父層以 ``:key="character.id"`` 重掛 —— 切換角色時整片 fresh mount，
 * 內層 editor 不必各自再 key。各 inner editor 只 PATCH 自己的欄位（tri-state，
 * 省略 = 不動），所以同一個 character 上掛多個 editor **不會互相覆寫**；某段
 * 存檔後透過 ``patch`` / ``@updated`` 把最新 character 往上傳，讓 picker 清單與
 * 其他唯讀引用（如 StoryArcPanel 的 world_frame 相容提示）保持同步。
 *
 * 不負責 B 層（voice / loras / provider·image·video profile 路由）—— 那些是
 * 部署綁定、不隨角色卡走，維持在各自 admin page。
 */
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import CollapsibleSection from '@/components/CollapsibleSection.vue'
import CharacterEditPanel from '@/components/CharacterEditPanel.vue'
import DispositionAdminEditor from '@/components/admin/DispositionAdminEditor.vue'
import WorldAdminEditor from '@/components/admin/WorldAdminEditor.vue'
import ProactiveAdminEditor from '@/components/admin/ProactiveAdminEditor.vue'
import StoryArcPanel from '@/components/StoryArcPanel.vue'
import CharacterImagesPanel from '@/components/CharacterImagesPanel.vue'

const props = defineProps<{
  character: Character
  characters?: Character[]
  showToolSettings?: boolean
}>()

const emit = defineEmits<{
  updated: [char: Character]
  'data-reset': [char: Character]
}>()

const { t } = useI18n()

function onUpdated(char: Character) {
  emit('updated', char)
}

function onDataReset(char: Character) {
  emit('data-reset', char)
}

/**
 * StoryArcPanel 只發 ``update:arc-template`` 意圖，綁定的 PATCH 由父層負責
 * （見其 emit 註解）—— 角色卡就是那個父層。寫回後把最新 character 往上傳，
 * 讓 ``character.arc_template_id`` 反映到 picker 與 StoryArcPanel 的 prop。
 */
async function onArcTemplate(templateId: string | null) {
  const updated = await updateCharacter(props.character.id, {
    arc_template_id: templateId,
  })
  emit('updated', updated)
}
</script>

<template>
  <div class="character-card-editor">
    <CollapsibleSection
      :title="t('admin.page.characters.card.identityTitle')"
      :hint="t('admin.page.characters.card.identityHint')"
      :default-open="true"
    >
      <CharacterEditPanel
        :character="character"
        :characters="characters"
        :show-tool-settings="showToolSettings"
        @updated="onUpdated"
        @data-reset="onDataReset"
      />
      <DispositionAdminEditor :character="character" :patch="onUpdated" />
    </CollapsibleSection>

    <CollapsibleSection
      :title="t('admin.page.characters.card.worldTitle')"
      :hint="t('admin.page.characters.card.worldHint')"
      :default-open="false"
    >
      <WorldAdminEditor :character="character" :patch="onUpdated" />
      <ProactiveAdminEditor :character="character" :patch="onUpdated" />
    </CollapsibleSection>

    <CollapsibleSection
      :title="t('admin.page.characters.card.storyTitle')"
      :hint="t('admin.page.characters.card.storyHint')"
      :default-open="false"
    >
      <StoryArcPanel
        :character-id="character.id"
        :arc-template-id="character.arc_template_id"
        :world-frame="character.world_frame"
        @update:arc-template="onArcTemplate"
      />
    </CollapsibleSection>

    <CollapsibleSection
      :title="t('admin.page.characters.card.stageTitle')"
      :hint="t('admin.page.characters.card.stageHint')"
      :default-open="false"
    >
      <CharacterImagesPanel :character="character" @updated="onUpdated" />
    </CollapsibleSection>
  </div>
</template>

<style scoped>
.character-card-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
</style>
