<script setup lang="ts">
/**
 * 角色卡市集（M4，MVP）—— 列出隨 repo 出貨的 `.lumecard` 預設角色，
 * 一鍵安裝成一個全新角色（A 層設定 + 落地同捆 arc template，runtime 歸零）。
 *
 * 安裝走後端 `POST /character-cards/{id}/install`，與手動匯入同一條路徑；
 * 成功後 emit ``installed`` 讓父頁刷新角色 picker。市集來源目前是 repo
 * 出貨；遠端 registry 列在 backlog（見 docs/CHARACTER_CARD_PLAN.md §8）。
 */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import type { Character } from '@/types/character'
import {
  listCharacterCards,
  installCharacterCard,
  type CharacterCardPackSummary,
} from '@/utils/api/characters'
import { UiCard, UiButton, UiBadge } from '@/components/ui'

const emit = defineEmits<{
  installed: [char: Character]
}>()

const { t } = useI18n()

const packs = ref<CharacterCardPackSummary[]>([])
const loading = ref(true)
const loadError = ref<string | null>(null)
const installingId = ref<string | null>(null)
// "翻成我的語言" — bundled packs ship zh-TW; an en/ja admin can opt into
// LLM-translating the A-layer profile + bundled arc templates on install
// (same flag the player gallery already sends). Off by default.
const translateOnInstall = ref(false)

async function load() {
  loading.value = true
  loadError.value = null
  try {
    packs.value = await listCharacterCards()
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

async function install(pack: CharacterCardPackSummary) {
  installingId.value = pack.pack_id
  try {
    const { character, landed_arc_template_ids } = await installCharacterCard(
      pack.pack_id,
      { translate: translateOnInstall.value },
    )
    notification.success({
      message: t('admin.page.characters.marketplace.installSuccess', { name: character.name }),
      description: landed_arc_template_ids.length
        ? t('admin.page.characters.importCardTemplates', { count: landed_arc_template_ids.length })
        : undefined,
    })
    emit('installed', character)
  } catch (err) {
    notification.error({
      message: t('admin.page.characters.marketplace.installError'),
      description: err instanceof Error ? err.message : String(err),
    })
  } finally {
    installingId.value = null
  }
}

onMounted(load)
</script>

<template>
  <UiCard>
    <template #header>
      <h2 class="marketplace__title">{{ t('admin.page.characters.marketplace.title') }}</h2>
    </template>

    <p class="marketplace__hint">{{ t('admin.page.characters.marketplace.hint') }}</p>

    <label class="marketplace__translate" :title="t('admin.page.characters.marketplace.translateHint')">
      <input type="checkbox" v-model="translateOnInstall" />
      <span>{{ t('admin.page.characters.marketplace.translateLabel') }}</span>
    </label>

    <p v-if="loading" class="marketplace__state">{{ t('common.state.loading') }}</p>
    <p v-else-if="loadError" class="marketplace__error">{{ loadError }}</p>
    <p v-else-if="packs.length === 0" class="marketplace__state">
      {{ t('admin.page.characters.marketplace.empty') }}
    </p>

    <div v-else class="marketplace__grid">
      <article v-for="pack in packs" :key="pack.pack_id" class="marketplace__pack">
        <div class="marketplace__pack-head">
          <h3 class="marketplace__pack-title">{{ pack.title }}</h3>
          <span v-if="pack.author" class="marketplace__pack-author">{{ pack.author }}</span>
        </div>

        <p v-if="pack.description" class="marketplace__pack-desc">{{ pack.description }}</p>

        <div v-if="pack.tags.length" class="marketplace__tags">
          <UiBadge v-for="tag in pack.tags" :key="tag" variant="default">{{ tag }}</UiBadge>
        </div>

        <div class="marketplace__meta">
          <span v-if="pack.bundled_arc_template_count">
            {{ t('admin.page.characters.marketplace.arcCount', { count: pack.bundled_arc_template_count }) }}
          </span>
          <span v-if="pack.stage_image_count">
            {{ t('admin.page.characters.marketplace.imageCount', { count: pack.stage_image_count }) }}
          </span>
        </div>

        <p v-if="pack.note" class="marketplace__note">{{ pack.note }}</p>

        <UiButton
          variant="primary"
          size="sm"
          block
          :loading="installingId === pack.pack_id"
          @click="install(pack)"
        >
          {{ t('admin.page.characters.marketplace.installAction') }}
        </UiButton>
      </article>
    </div>
  </UiCard>
</template>

<style scoped>
.marketplace__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.marketplace__hint {
  margin: 0 0 var(--space-3);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.marketplace__translate {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-3);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  user-select: none;
}
.marketplace__translate input {
  accent-color: var(--color-spark);
  cursor: pointer;
}
.marketplace__state,
.marketplace__error {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.marketplace__error {
  color: #f4a3a3;
}
.marketplace__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--space-3);
}
.marketplace__pack {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
}
.marketplace__pack-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.marketplace__pack-title {
  margin: 0;
  font-size: var(--font-sm);
  font-weight: 600;
}
.marketplace__pack-author {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  flex-shrink: 0;
}
.marketplace__pack-desc {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.marketplace__tags {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}
.marketplace__meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.marketplace__note {
  margin: 0;
  padding: var(--space-2);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  line-height: 1.5;
}
</style>
