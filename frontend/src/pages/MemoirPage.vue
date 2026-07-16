<script setup lang="ts">
/**
 * 直接路由 ``/memoir/:characterId`` 的薄殼。日常使用者多半從 PlayerSidebar
 * 開 MemoirOverlay；本頁是給直連 URL / 嵌入測試 / SEO 留的 fallback。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import MemoirContent from '@/components/memoir/MemoirContent.vue'
import { UiButton } from '@/components/ui'
import { getCharacter } from '@/utils/api/characters'
import type { Character } from '@/types/character'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const characterId = computed(() => {
  const raw = route.params.characterId
  return Array.isArray(raw) ? raw[0] : raw
})

const character = ref<Character | null>(null)

const loadCharacter = async () => {
  if (!characterId.value) {
    character.value = null
    return
  }
  try {
    character.value = await getCharacter(characterId.value)
  }
  catch {
    character.value = null
  }
}

onMounted(loadCharacter)

const portraitUrl = computed(() => character.value?.image_urls?.[0] ?? null)
</script>

<template>
  <div class="memoir-page">
    <div class="memoir-page__inner">
      <header class="memoir-page__header">
        <UiButton variant="ghost" size="sm" @click="router.push('/')">
          {{ t('memoir.backToStage') }}
        </UiButton>
        <div class="memoir-page__title-block">
          <h1 class="memoir-page__title">{{ t('memoir.title') }}</h1>
          <div v-if="character" class="memoir-page__character">
            <span
              v-if="portraitUrl"
              class="memoir-page__avatar"
              :style="{ backgroundImage: `url(${portraitUrl})` }"
              aria-hidden="true"
            />
            <span class="memoir-page__char-name">{{ character.name }}</span>
          </div>
          <span v-else-if="characterId" class="memoir-page__char-id">
            {{ t('memoir.characterIdLabel') }}<code>{{ characterId }}</code>
          </span>
          <span v-else class="memoir-page__char-id">{{ t('memoir.noCharacter') }}</span>
        </div>
      </header>

      <MemoirContent :character-id="characterId" />
    </div>
  </div>
</template>

<style scoped>
.memoir-page {
  position: relative;
  height: 100%;
  overflow-y: auto;
  background-color: #0c0820;
  color: var(--color-text);
}
.memoir-page::before {
  content: '';
  position: fixed;
  inset: 0;
  background-image:
    radial-gradient(circle at 50% 0%, rgba(139, 92, 246, 0.18), transparent 60%),
    url('/memoir/bg_mem.png');
  background-size: cover, cover;
  background-position: center, center;
  background-repeat: no-repeat, no-repeat;
  opacity: 0.85;
  pointer-events: none;
  z-index: 0;
}
.memoir-page > * {
  position: relative;
  z-index: 1;
}
.memoir-page__inner {
  max-width: 1280px;
  margin: 0 auto;
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.memoir-page__header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}
.memoir-page__title-block {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.memoir-page__title {
  margin: 0;
  font-size: var(--font-xl);
  font-weight: 600;
}
.memoir-page__character {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.memoir-page__avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background-size: cover;
  background-position: center;
  border: 2px solid rgba(139, 92, 246, 0.45);
}
.memoir-page__char-name {
  font-size: var(--font-md);
  color: var(--color-text-secondary);
}
.memoir-page__char-id {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
