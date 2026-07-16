<script setup lang="ts">
// 分享圖卡（Creator Studio C0-4）。
// 隱私紅線：摘句採白名單式選擇（預設不選、絕不自動帶出未選內容）、
// 角色名/立繪可遮、分享前預覽即最終輸出、明示內容將離開私人空間。
// 轉化層：X share intent 預設文案帶 landing 連結（utm 歸因）。
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { FusionStory } from '@/types/fusionStory'

const props = defineProps<{
  story: FusionStory
  characters: Character[]
}>()

const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()

const CARD_WIDTH = 1200
const CARD_HEIGHT = 675
const LANDING_URL =
  'https://yuralume.com/?utm_source=share&utm_medium=story_card&utm_campaign=creator_studio'

const selectedIndex = ref<number | null>(null)
const showPortrait = ref(true)
const showNames = ref(true)
const watermark = ref(true)
const canvasEl = ref<HTMLCanvasElement | null>(null)
const portraitImage = ref<HTMLImageElement | null>(null)

const excerpts = computed(() =>
  (props.story.full_text || '')
    .split(/\n{2,}/)
    .map((p) => p.trim())
    .filter((p) => p.length >= 10),
)

const castNames = computed(() => {
  const byId: Record<string, Character> = {}
  for (const c of props.characters) byId[c.id] = c
  return props.story.character_ids
    .map((id) => byId[id]?.name || '')
    .filter(Boolean)
})

const portraitUrl = computed(() => {
  const byId: Record<string, Character> = {}
  for (const c of props.characters) byId[c.id] = c
  for (const cid of props.story.character_ids) {
    const urls = byId[cid]?.image_urls
    if (urls?.length) return urls[0]
  }
  return null
})

const canRender = computed(() => selectedIndex.value != null)

function loadPortrait() {
  portraitImage.value = null
  const url = portraitUrl.value
  if (!url) return
  const img = new Image()
  // 立繪可能在同源 /uploads 或外部 object storage；anonymous 失敗時
  // 直接放棄立繪（不讓 canvas 被污染而導致下載失敗）。
  img.crossOrigin = 'anonymous'
  img.onload = () => {
    portraitImage.value = img
    void renderCard()
  }
  img.onerror = () => {
    portraitImage.value = null
    void renderCard()
  }
  img.src = url
}

function wrapText(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
  maxLines: number,
): string[] {
  const lines: string[] = []
  let current = ''
  for (const ch of text.replace(/\s+/g, (m) => (m.includes('\n') ? '\n' : m[0]))) {
    if (ch === '\n') {
      lines.push(current)
      current = ''
      continue
    }
    const attempt = current + ch
    if (ctx.measureText(attempt).width > maxWidth && current) {
      lines.push(current)
      current = ch
    } else {
      current = attempt
    }
    if (lines.length >= maxLines) break
  }
  if (current && lines.length < maxLines) lines.push(current)
  if (lines.length >= maxLines) {
    const last = lines[maxLines - 1]
    lines[maxLines - 1] = `${last.slice(0, Math.max(0, last.length - 1))}…`
    return lines.slice(0, maxLines)
  }
  return lines
}

async function renderCard() {
  await nextTick()
  const canvas = canvasEl.value
  if (!canvas) return
  const ctx = canvas.getContext('2d')
  if (!ctx) return

  // 背景：自帶深色漸層，輸出圖在任何深淺色環境都可讀。
  const gradient = ctx.createLinearGradient(0, 0, CARD_WIDTH, CARD_HEIGHT)
  gradient.addColorStop(0, '#171e33')
  gradient.addColorStop(1, '#2a1e3f')
  ctx.fillStyle = gradient
  ctx.fillRect(0, 0, CARD_WIDTH, CARD_HEIGHT)

  ctx.strokeStyle = 'rgba(255, 255, 255, 0.14)'
  ctx.lineWidth = 2
  ctx.strokeRect(28, 28, CARD_WIDTH - 56, CARD_HEIGHT - 56)

  const hasPortrait = showPortrait.value && portraitImage.value != null
  const textRight = hasPortrait ? CARD_WIDTH - 360 : CARD_WIDTH - 96
  const textWidth = textRight - 96

  ctx.fillStyle = 'rgba(255, 255, 255, 0.92)'
  ctx.font = 'bold 44px "Noto Sans TC", "Microsoft JhengHei", sans-serif'
  const titleLines = wrapText(ctx, props.story.title, textWidth, 2)
  let y = 120
  for (const line of titleLines) {
    ctx.fillText(line, 96, y)
    y += 56
  }

  const excerpt =
    selectedIndex.value != null ? excerpts.value[selectedIndex.value] : ''
  ctx.fillStyle = 'rgba(255, 255, 255, 0.78)'
  ctx.font = '26px "Noto Sans TC", "Microsoft JhengHei", sans-serif'
  y += 16
  for (const line of wrapText(ctx, excerpt, textWidth, 9)) {
    ctx.fillText(line, 96, y)
    y += 42
  }

  if (showNames.value && castNames.value.length) {
    ctx.fillStyle = 'rgba(198, 178, 255, 0.85)'
    ctx.font = '22px "Noto Sans TC", "Microsoft JhengHei", sans-serif'
    ctx.fillText(
      castNames.value.join(' × '),
      96,
      CARD_HEIGHT - 88,
    )
  }

  ctx.fillStyle = 'rgba(255, 200, 97, 0.95)'
  ctx.font = 'bold 24px "Noto Sans TC", sans-serif'
  ctx.fillText('Yuralume', 96, CARD_HEIGHT - 52)

  if (watermark.value) {
    ctx.fillStyle = 'rgba(255, 255, 255, 0.4)'
    ctx.font = '20px sans-serif'
    const mark = 'yuralume.com'
    const markWidth = ctx.measureText(mark).width
    ctx.fillText(mark, CARD_WIDTH - 96 - markWidth, CARD_HEIGHT - 52)
  }

  if (hasPortrait && portraitImage.value) {
    const img = portraitImage.value
    const size = 300
    const x = CARD_WIDTH - 96 - size
    const py = (CARD_HEIGHT - size) / 2
    ctx.save()
    ctx.beginPath()
    ctx.roundRect(x, py, size, size, 24)
    ctx.clip()
    const scale = Math.max(size / img.width, size / img.height)
    const dw = img.width * scale
    const dh = img.height * scale
    ctx.drawImage(img, x + (size - dw) / 2, py + (size - dh) / 2, dw, dh)
    ctx.restore()
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.25)'
    ctx.lineWidth = 2
    ctx.beginPath()
    ctx.roundRect(x, py, size, size, 24)
    ctx.stroke()
  }
}

function handleDownload() {
  const canvas = canvasEl.value
  if (!canvas || !canRender.value) return
  canvas.toBlob((blob) => {
    if (!blob) return
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${props.story.title || 'yuralume-story'}.png`
    anchor.click()
    URL.revokeObjectURL(url)
  }, 'image/png')
}

function handleShareToX() {
  if (!canRender.value) return
  const text = t('fusionStory.shareCard.shareText', {
    title: props.story.title,
  })
  const intent = new URL('https://twitter.com/intent/tweet')
  intent.searchParams.set('text', text)
  intent.searchParams.set('url', LANDING_URL)
  window.open(intent.toString(), '_blank', 'noopener')
}

watch([selectedIndex, showNames, watermark], () => void renderCard())
watch(showPortrait, () => void renderCard())

onMounted(() => {
  loadPortrait()
  void renderCard()
})
</script>

<template>
  <div class="share-card__backdrop" @click.self="emit('close')">
    <div class="share-card glass-panel" role="dialog" aria-modal="true">
      <header class="share-card__header">
        <h3>{{ t('fusionStory.shareCard.modalTitle') }}</h3>
        <button
          class="share-card__close"
          :aria-label="t('common.actions.cancel')"
          @click="emit('close')"
        >
          ×
        </button>
      </header>

      <p class="share-card__privacy">
        {{ t('fusionStory.shareCard.privacyNotice') }}
      </p>

      <div class="share-card__body">
        <section class="share-card__excerpts">
          <h4 class="share-card__label">
            {{ t('fusionStory.shareCard.excerptLabel') }}
          </h4>
          <label
            v-for="(paragraph, idx) in excerpts"
            :key="idx"
            class="share-card__excerpt"
            :class="{ 'is-selected': selectedIndex === idx }"
          >
            <input
              v-model="selectedIndex"
              type="radio"
              name="share-excerpt"
              :value="idx"
            />
            <span>{{ paragraph }}</span>
          </label>
          <p v-if="!excerpts.length" class="share-card__empty">
            {{ t('fusionStory.shareCard.noExcerpts') }}
          </p>
        </section>

        <section class="share-card__right">
          <div class="share-card__options">
            <label>
              <input v-model="showPortrait" type="checkbox" />
              <span>{{ t('fusionStory.shareCard.optionPortrait') }}</span>
            </label>
            <label>
              <input v-model="showNames" type="checkbox" />
              <span>{{ t('fusionStory.shareCard.optionNames') }}</span>
            </label>
            <label>
              <input v-model="watermark" type="checkbox" />
              <span>{{ t('fusionStory.shareCard.optionWatermark') }}</span>
            </label>
          </div>

          <div class="share-card__preview">
            <canvas
              v-show="canRender"
              ref="canvasEl"
              :width="CARD_WIDTH"
              :height="CARD_HEIGHT"
            />
            <p v-if="!canRender" class="share-card__placeholder">
              {{ t('fusionStory.shareCard.selectPrompt') }}
            </p>
          </div>

          <div class="share-card__actions">
            <button
              class="share-card__btn share-card__btn--primary"
              :disabled="!canRender"
              @click="handleDownload"
            >
              {{ t('fusionStory.shareCard.download') }}
            </button>
            <button
              class="share-card__btn"
              :disabled="!canRender"
              @click="handleShareToX"
            >
              {{ t('fusionStory.shareCard.shareX') }}
            </button>
          </div>
          <p class="share-card__hint">
            {{ t('fusionStory.shareCard.shareHint') }}
          </p>
        </section>
      </div>
    </div>
  </div>
</template>

<style scoped>
.share-card__backdrop {
  position: fixed;
  inset: 0;
  z-index: 60;
  background: rgba(8, 10, 18, 0.72);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
.share-card {
  width: min(1080px, 100%);
  max-height: 92vh;
  overflow-y: auto;
  border-radius: 10px;
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.share-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.share-card__header h3 {
  margin: 0;
  font-size: 18px;
}
.share-card__close {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 22px;
  cursor: pointer;
  line-height: 1;
  padding: 4px 8px;
}
.share-card__privacy {
  margin: 0;
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid rgba(255, 200, 97, 0.4);
  background: rgba(255, 200, 97, 0.08);
  color: rgb(255, 214, 150);
  font-size: 13px;
}
.share-card__body {
  display: grid;
  grid-template-columns: minmax(260px, 1fr) minmax(320px, 1.4fr);
  gap: 14px;
  min-height: 0;
}
.share-card__label {
  margin: 0 0 6px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}
.share-card__excerpts {
  max-height: 480px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-right: 4px;
}
.share-card__excerpt {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 8px 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  cursor: pointer;
  font-size: 13px;
  line-height: 1.7;
}
.share-card__excerpt.is-selected {
  border-color: rgba(var(--color-primary-rgb), 0.65);
  background: rgba(var(--color-primary-rgb), 0.12);
}
.share-card__excerpt input {
  margin-top: 4px;
  flex: 0 0 auto;
}
.share-card__empty {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.5);
}
.share-card__right {
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}
.share-card__options {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 13px;
}
.share-card__options label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.share-card__preview {
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.3);
  min-height: 200px;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}
.share-card__preview canvas {
  width: 100%;
  height: auto;
  display: block;
}
.share-card__placeholder {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.5);
  padding: 24px;
}
.share-card__actions {
  display: flex;
  gap: 8px;
}
.share-card__btn {
  background: rgba(255, 255, 255, 0.06);
  color: inherit;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 4px;
  padding: 8px 14px;
  cursor: pointer;
}
.share-card__btn--primary {
  background: rgba(var(--color-primary-rgb), 0.25);
  border-color: rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
}
.share-card__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.share-card__hint {
  margin: 0;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}

@media (max-width: 860px) {
  .share-card__body {
    grid-template-columns: 1fr;
  }
  .share-card__excerpts {
    max-height: 220px;
  }
}
</style>
