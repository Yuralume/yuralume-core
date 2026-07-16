<script setup lang="ts">
/**
 * LumeGram — 浮在主舞台上的全局動態牆 overlay。
 *
 * 行為：
 *
 * - 開啟瞬間預設全局模式（``filterId = null``）：FeedPanel 拉所有角色
 *   的混合 timeline；每張卡頂部顯示角色頭像 + 名字。
 * - 點角色頭像 / 名字 → overlay 切到 filter 模式，只顯示該角色的動態，
 *   header 改成「← @角色名」可按返回回到全局。不會切換主舞台的選中角色。
 * - 每次開啟 overlay 都 advance localStorage watermark + dispatch
 *   ``kokoro:feed-watermark-bumped`` event，讓 StagePage 紅點歸零。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import FeedPanel from './FeedPanel.vue'

const { t } = useI18n()

const FEED_WATERMARK_KEY = 'kokoro.feedLastViewedAt'

const props = defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

// null = 全局；string = filter 到該角色。每次重新打開 overlay 都會
// reset 回 null（使用者預期：點相機 icon = 從頭看新的）。
const filterId = ref<string | null>(null)
// 全局→filter 切換時要拿來顯示 header 名字。從 FeedPanel emit 上來時
// 同步寫進來，避免再去查一次 character API。
const filterName = ref<string | null>(null)

const headerTitle = computed(() => filterId.value
  ? (filterName.value ?? t('common.fallback.character'))
  : 'LumeGram',
)

function handleSelectCharacter(characterId: string) {
  filterId.value = characterId
  // FeedPanel 內部已經 cache 了 character map；但這層拿不到，所以
  // 暫時用 id 占位，FeedPanel 重新 mount filter 模式時會用 prop
  // 傳進去的 ``character-name`` — 沒有也沒關係，header 用 id 撐著。
  filterName.value = null
  // 異步補抓名字以便 header 顯示中文名
  void resolveFilterName(characterId)
}

async function resolveFilterName(characterId: string) {
  try {
    const { getCharacter } = await import('@/utils/api/characters')
    const c = await getCharacter(characterId)
    if (filterId.value === characterId) {
      filterName.value = c.name
    }
  } catch {
    // 抓不到就用 id 撐
  }
}

function backToGlobal() {
  filterId.value = null
  filterName.value = null
}

function handleKey(ev: KeyboardEvent) {
  if (!props.open) return
  if (ev.key === 'Escape') {
    if (filterId.value) {
      // filter 模式下 Esc 先回全局，再次 Esc 才關 overlay — 跟 IG 行為一致
      backToGlobal()
    } else {
      emit('close')
    }
  }
}

function applyBodyLock(locked: boolean) {
  if (typeof document === 'undefined') return
  document.body.style.overflow = locked ? 'hidden' : ''
}

function bumpWatermark() {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(FEED_WATERMARK_KEY, new Date().toISOString())
    window.dispatchEvent(new CustomEvent('kokoro:feed-watermark-bumped'))
  } catch {
    // 隱私模式 / quota 滿 — 紅點不算 critical
  }
}

watch(() => props.open, (next, prev) => {
  applyBodyLock(next)
  if (next && !prev) {
    // 每次重新開啟都從全局開始 + 標記已讀
    filterId.value = null
    filterName.value = null
    bumpWatermark()
  }
})

onMounted(() => {
  window.addEventListener('keydown', handleKey)
  if (props.open) {
    applyBodyLock(true)
    bumpWatermark()
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleKey)
  applyBodyLock(false)
})
</script>

<template>
  <Transition name="kg-fade">
    <div
      v-if="open"
      class="kg-overlay"
      role="dialog"
      aria-modal="true"
      :aria-label="t('kokoroGram.overlayTitle')"
      @click.self="emit('close')"
    >
      <div class="kg-frame">
        <header class="kg-header">
          <button
            v-if="filterId"
            class="kg-back"
            type="button"
            :aria-label="t('kokoroGram.back')"
            :title="t('kokoroGram.back')"
            @click="backToGlobal"
          >◀</button>
          <div class="kg-title">
            <img
              v-if="!filterId"
              src="/LumeGramLogo.png"
              alt=""
              class="kg-logo"
              aria-hidden="true"
            />
            <span v-if="filterId" class="kg-character-prefix">@</span>
            <span :class="filterId ? 'kg-character' : 'kg-brand'">
              {{ headerTitle }}
            </span>
          </div>
          <button
            class="kg-close"
            type="button"
            :aria-label="t('kokoroGram.close')"
            @click="emit('close')"
          >×</button>
        </header>
        <div class="kg-body">
          <FeedPanel
            :character-id="filterId"
            :character-name="filterName"
            @select-character="handleSelectCharacter"
          />
        </div>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
.kg-overlay {
  position: fixed;
  inset: 0;
  z-index: 400;
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(8px) saturate(1.1);
  -webkit-backdrop-filter: blur(8px) saturate(1.1);
  display: flex;
  align-items: stretch;
  justify-content: center;
  padding: 0;
}

.kg-frame {
  display: flex;
  flex-direction: column;
  width: 100%;
  max-width: 480px;
  height: 100%;
  background: var(--color-bg-secondary);
  border-left: 1px solid var(--color-border);
  border-right: 1px solid var(--color-border);
  box-shadow: 0 0 40px rgba(0, 0, 0, 0.5);
  overflow: hidden;
}

.kg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  padding: 10px 14px;
  padding-top: calc(10px + var(--safe-area-top));
  border-bottom: 1px solid var(--color-border);
  background: linear-gradient(
    90deg,
    rgba(238, 119, 82, 0.08),
    rgba(231, 60, 126, 0.08),
    rgba(35, 166, 213, 0.08)
  );
  flex-shrink: 0;
}

.kg-back {
  background: transparent;
  border: 1px solid transparent;
  color: var(--color-text-secondary);
  font-size: 14px;
  line-height: 1;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}

.kg-back:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

.kg-title {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  flex: 1;
}

.kg-logo {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  object-fit: cover;
  flex-shrink: 0;
  box-shadow: 0 0 12px rgba(221, 42, 123, 0.35);
}

.kg-brand {
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
  letter-spacing: 0;
}

.kg-character-prefix {
  color: var(--color-text-secondary);
  font-size: 14px;
  font-weight: 500;
}

.kg-character {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.kg-close {
  background: transparent;
  border: 1px solid transparent;
  color: var(--color-text-secondary);
  font-size: 22px;
  line-height: 1;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  cursor: pointer;
  flex-shrink: 0;
  transition: background 0.15s, color 0.15s;
}

.kg-close:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

.kg-body {
  flex: 1;
  overflow-y: auto;
  padding: 12px 14px;
  padding-bottom: calc(12px + var(--safe-area-bottom));
}

.kg-fade-enter-active,
.kg-fade-leave-active {
  transition: opacity 0.2s ease;
}
.kg-fade-enter-active .kg-frame,
.kg-fade-leave-active .kg-frame {
  transition: transform 0.25s ease;
}

.kg-fade-enter-from,
.kg-fade-leave-to {
  opacity: 0;
}
.kg-fade-enter-from .kg-frame,
.kg-fade-leave-to .kg-frame {
  transform: translateY(16px);
}

@media (max-width: 520px) {
  .kg-frame {
    max-width: none;
    border-left: none;
    border-right: none;
  }
}
</style>
