<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { LeftOutlined, RightOutlined } from '@ant-design/icons-vue'
import type { CharacterCardPreview } from '@/utils/api/characters'

// 瀏覽舞台左右兩側的「上一張 / 下一張」迷你卡，點擊即切換到該卡。
// 刻意只呈現主圖 + 名稱：side peek 是次要資訊，互動入口由 aria-label 與 chevron 提示。
const props = defineProps<{
  card: CharacterCardPreview
  side: 'prev' | 'next'
  navLabel: string
}>()

const emit = defineEmits<{
  select: []
}>()

const failed = ref(false)
const image = computed(() => props.card.image_urls[0] ?? '')
const showImage = computed(() => Boolean(image.value) && !failed.value)
const title = computed(() => props.card.name || props.card.title)
const initial = computed(() => (title.value || '?').trim().charAt(0).toUpperCase())

watch(() => props.card, () => {
  failed.value = false
})
</script>

<template>
  <button
    type="button"
    class="character-card-thumb"
    :class="`character-card-thumb--${side}`"
    :aria-label="navLabel"
    @click="emit('select')"
  >
    <span class="character-card-thumb__chevron" aria-hidden="true">
      <LeftOutlined v-if="side === 'prev'" />
      <RightOutlined v-else />
    </span>
    <span class="character-card-thumb__art">
      <img
        v-if="showImage"
        class="character-card-thumb__image"
        :src="image"
        :alt="title"
        @error="failed = true"
      />
      <span v-else class="character-card-thumb__fallback" aria-hidden="true">
        {{ initial }}
      </span>
    </span>
    <span class="character-card-thumb__name">{{ title }}</span>
  </button>
</template>

<style scoped>
/* 迷你卡：呼應主卡的 foil 邊框質感，但壓暗、縮小，作為「後方那張卡」的暗示。 */
.character-card-thumb {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
  width: clamp(92px, 13vw, 132px);
  padding: 5px;
  border: 0;
  border-radius: 13px;
  background:
    linear-gradient(
      150deg,
      rgba(255, 209, 128, 0.45),
      rgba(201, 143, 219, 0.32) 40%,
      rgba(139, 109, 255, 0.3) 100%
    );
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.36);
  cursor: pointer;
  opacity: 0.52;
  transform: scale(0.92);
  transition: opacity 0.24s ease, transform 0.24s ease, box-shadow 0.24s ease;
}

.character-card-thumb--prev {
  transform: scale(0.92) translateX(6px);
}

.character-card-thumb--next {
  transform: scale(0.92) translateX(-6px);
}

.character-card-thumb:hover,
.character-card-thumb:focus-visible {
  opacity: 1;
  outline: none;
  box-shadow:
    0 16px 38px rgba(0, 0, 0, 0.48),
    0 0 0 1px rgba(255, 209, 128, 0.45);
}

.character-card-thumb--prev:hover,
.character-card-thumb--prev:focus-visible {
  transform: scale(0.98) translateX(0);
}

.character-card-thumb--next:hover,
.character-card-thumb--next:focus-visible {
  transform: scale(0.98) translateX(0);
}

.character-card-thumb__art {
  position: relative;
  aspect-ratio: 3 / 4;
  border-radius: 8px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.04);
  box-shadow:
    0 0 0 1px rgba(255, 255, 255, 0.14) inset,
    0 6px 14px rgba(0, 0, 0, 0.38);
}

.character-card-thumb__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.character-card-thumb__fallback {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(circle at 36% 24%, rgba(255, 209, 128, 0.4), transparent 38%),
    linear-gradient(135deg, rgba(95, 213, 164, 0.22), rgba(139, 109, 255, 0.28)),
    rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 34px;
  line-height: 1;
}

.character-card-thumb__name {
  padding: 0 2px;
  color: var(--color-text);
  font-size: var(--font-xs);
  line-height: 1.3;
  text-align: center;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 方向徽章：浮在外緣，提示這是「上一張 / 下一張」的入口。 */
.character-card-thumb__chevron {
  position: absolute;
  top: 50%;
  z-index: 2;
  display: grid;
  place-items: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  border: 1px solid rgba(255, 255, 255, 0.22);
  background: rgba(17, 13, 36, 0.72);
  color: var(--color-text);
  font-size: 12px;
  transform: translateY(-50%);
  transition: background 0.2s ease, border-color 0.2s ease;
}

.character-card-thumb--prev .character-card-thumb__chevron {
  left: -10px;
}

.character-card-thumb--next .character-card-thumb__chevron {
  right: -10px;
}

.character-card-thumb:hover .character-card-thumb__chevron,
.character-card-thumb:focus-visible .character-card-thumb__chevron {
  background: rgba(255, 209, 128, 0.22);
  border-color: rgba(255, 209, 128, 0.55);
}

@media (prefers-reduced-motion: reduce) {
  .character-card-thumb,
  .character-card-thumb__chevron {
    transition: none;
  }
}
</style>
