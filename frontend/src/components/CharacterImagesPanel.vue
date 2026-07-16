<script setup lang="ts">
import { computed, ref, watch, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import {
  commitPortraitCandidates,
  deleteCharacterImage,
  generatePortraitCandidates,
  reorderCharacterImages,
  uploadCharacterImage,
  type PortraitAspect,
} from '@/utils/api/characters'
import { transferStageToAlbum } from '@/utils/api/album'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  character: Character
}>()

const emit = defineEmits<{
  updated: [char: Character]
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const uploading = ref(false)
const busyUrl = ref<string | null>(null)
const errorMsg = ref<string | null>(null)

const generating = ref(false)
const committing = ref(false)
const generatePrompt = ref('')
const generateAspect = ref<PortraitAspect>('portrait')
const generateCount = ref<number>(4)

// Gacha flow — once candidates are generated they hang here until
// the user commits or discards. The main image library stays
// untouched until commit, so "cancel" is just dropping this state.
//
// Each candidate has a tri-state destination: 'discard' (default),
// 'stage' (promote to carousel), or 'album' (skip stage, archive
// directly). Click cycles: discard → stage → album → discard.
type CandidateTarget = 'discard' | 'stage' | 'album'
const candidateUrls = ref<string[]>([])
const candidateTargets = ref<Map<string, CandidateTarget>>(new Map())

const stageCount = computed(() =>
  Array.from(candidateTargets.value.values()).filter((t) => t === 'stage').length,
)
const albumCount = computed(() =>
  Array.from(candidateTargets.value.values()).filter((t) => t === 'album').length,
)
const commitCount = computed(() => stageCount.value + albumCount.value)

async function handleFilePick(event: Event) {
  const input = event.target as HTMLInputElement
  const files = input.files ? Array.from(input.files) : []
  if (!files.length) return
  input.value = '' // allow picking same file again later

  uploading.value = true
  errorMsg.value = null
  try {
    let latest: Character = props.character
    for (const file of files) {
      latest = await uploadCharacterImage(props.character.id, file)
    }
    emit('updated', latest)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.uploadFailed')
  } finally {
    uploading.value = false
  }
}

async function handleDelete(url: string) {
  if (!await confirmDialog({
    content: t('characterImagesPanel.confirm.delete'),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  busyUrl.value = url
  errorMsg.value = null
  try {
    const updated = await deleteCharacterImage(props.character.id, url)
    emit('updated', updated)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.deleteFailed')
  } finally {
    busyUrl.value = null
  }
}

async function handleArchive(url: string) {
  // 舞台 → 相簿：只換索引、不動檔案。使用者常見流程是「生太多了想
  // 清舞台但捨不得刪」，相簿剛好承接。
  if (!await confirmDialog({
    content: t('characterImagesPanel.confirm.archive'),
  })) return
  busyUrl.value = url
  errorMsg.value = null
  try {
    const updated = await transferStageToAlbum(props.character.id, url)
    emit('updated', updated)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.archiveFailed')
  } finally {
    busyUrl.value = null
  }
}

async function handleGenerate() {
  const positive = generatePrompt.value.trim()
  if (!positive) {
    errorMsg.value = t('characterImagesPanel.errors.promptRequired')
    return
  }
  generating.value = true
  errorMsg.value = null
  try {
    const res = await generatePortraitCandidates(
      props.character.id, positive, generateAspect.value, generateCount.value,
    )
    candidateUrls.value = res.candidates
    // Default: every candidate pre-selected for stage — saves a click
    // when user wants to keep them all. Click cycles into album
    // or discard per-tile.
    const fresh = new Map<string, CandidateTarget>()
    for (const url of res.candidates) fresh.set(url, 'stage')
    candidateTargets.value = fresh
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.generateFailed')
  } finally {
    generating.value = false
  }
}

function cycleCandidate(url: string) {
  const next = new Map(candidateTargets.value)
  const current = next.get(url) ?? 'discard'
  const order: CandidateTarget[] = ['discard', 'stage', 'album']
  const nextTarget = order[(order.indexOf(current) + 1) % order.length]
  next.set(url, nextTarget)
  candidateTargets.value = next
}

function setAllTargets(target: CandidateTarget) {
  const next = new Map<string, CandidateTarget>()
  for (const url of candidateUrls.value) next.set(url, target)
  candidateTargets.value = next
}

async function commitSelected() {
  const keepUrls: string[] = []
  const albumUrls: string[] = []
  for (const [url, target] of candidateTargets.value) {
    if (target === 'stage') keepUrls.push(url)
    else if (target === 'album') albumUrls.push(url)
  }
  committing.value = true
  errorMsg.value = null
  try {
    const updated = await commitPortraitCandidates(
      props.character.id, keepUrls, albumUrls,
    )
    emit('updated', updated)
    candidateUrls.value = []
    candidateTargets.value = new Map()
    generatePrompt.value = ''
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.commitFailed')
  } finally {
    committing.value = false
  }
}

async function discardAllCandidates() {
  committing.value = true
  errorMsg.value = null
  try {
    await commitPortraitCandidates(props.character.id, [], [])
    candidateUrls.value = []
    candidateTargets.value = new Map()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.discardFailed')
  } finally {
    committing.value = false
  }
}

async function move(index: number, delta: -1 | 1) {
  const next = index + delta
  const list = [...props.character.image_urls]
  if (next < 0 || next >= list.length) return
  ;[list[index], list[next]] = [list[next], list[index]]
  busyUrl.value = list[index]
  errorMsg.value = null
  try {
    const updated = await reorderCharacterImages(props.character.id, list)
    emit('updated', updated)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.reorderFailed')
  } finally {
    busyUrl.value = null
  }
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}

const candidateSummary = computed(() => t('characterImagesPanel.candidates.summary', {
  stage: stageCount.value,
  album: albumCount.value,
  discard: candidateUrls.value.length - commitCount.value,
}))

const commitButtonLabel = computed(() => {
  if (committing.value) return t('characterImagesPanel.candidates.processing')
  return t('characterImagesPanel.candidates.commitSelected', {
    stage: stageCount.value,
    album: albumCount.value,
  })
})

function candidateBadgeLabel(target: CandidateTarget): string {
  switch (target) {
    case 'stage':
      return t('characterImagesPanel.candidates.badges.stage')
    case 'album':
      return t('characterImagesPanel.candidates.badges.album')
    case 'discard':
    default:
      return t('characterImagesPanel.candidates.badges.discard')
  }
}

// 候選 modal 開啟時鎖 body 捲動 + 接 ESC 當作「全部捨棄」的捷徑
// （刻意不把點擊背景當成關閉，避免手滑刪掉剛生成的圖）
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (candidateUrls.value.length === 0) return
  if (committing.value) return
  event.preventDefault()
  discardAllCandidates()
}

watch(candidateUrls, (urls) => {
  if (typeof document === 'undefined') return
  document.body.style.overflow = urls.length > 0 ? 'hidden' : ''
})

if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onKeydown)
  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onKeydown)
    if (typeof document !== 'undefined') document.body.style.overflow = ''
  })
}
</script>

<template>
  <div class="images-panel">
    <div class="images-header">
      <h3 class="section-title">{{ t('characterImagesPanel.title') }}</h3>
      <p class="images-hint">
        {{ t('characterImagesPanel.hint') }}
      </p>
    </div>

    <div v-if="character.image_urls.length === 0" class="images-empty">
      {{ t('characterImagesPanel.empty') }}
    </div>
    <div v-else class="images-grid">
      <div
        v-for="(url, index) in character.image_urls"
        :key="url"
        class="image-tile"
      >
        <img :src="url" :alt="t('characterImagesPanel.imageAlt', { name: character.name, index: index + 1 })" />
        <div class="image-actions">
          <button
            class="tile-btn"
            :disabled="index === 0 || busyUrl === url"
            :title="t('characterImagesPanel.actions.movePrevious')"
            @click="move(index, -1)"
          >◀</button>
          <button
            class="tile-btn"
            :disabled="index === character.image_urls.length - 1 || busyUrl === url"
            :title="t('characterImagesPanel.actions.moveNext')"
            @click="move(index, 1)"
          >▶</button>
          <button
            class="tile-btn"
            :disabled="busyUrl === url"
            :title="t('characterImagesPanel.actions.archiveTitle')"
            @click="handleArchive(url)"
          >📁</button>
          <button
            class="tile-btn tile-btn-danger"
            :disabled="busyUrl === url"
            :title="t('common.actions.delete')"
            @click="handleDelete(url)"
          >×</button>
        </div>
        <span v-if="index === 0" class="primary-badge">{{ t('characterImagesPanel.primaryBadge') }}</span>
      </div>
    </div>

    <label :class="['upload-btn', { disabled: uploading }]">
      <input
        type="file"
        accept="image/*"
        multiple
        :disabled="uploading"
        @change="handleFilePick"
      />
      <span>{{ uploading ? t('characterImagesPanel.actions.uploading') : t('characterImagesPanel.actions.upload') }}</span>
    </label>

    <div class="generate-section">
      <div class="generate-title">{{ t('characterImagesPanel.generate.title') }}</div>
      <div class="generate-hint">
        {{ t('characterImagesPanel.generate.hint') }}
      </div>
      <textarea
        v-model="generatePrompt"
        class="field-textarea"
        rows="2"
        :placeholder="t('characterImagesPanel.generate.placeholder')"
        :disabled="generating || candidateUrls.length > 0"
      />
      <div class="generate-row">
        <select
          v-model="generateAspect"
          class="field-select"
          :disabled="generating || candidateUrls.length > 0"
        >
          <option value="portrait">{{ t('characterImagesPanel.generate.aspect.portrait') }}</option>
          <option value="landscape">{{ t('characterImagesPanel.generate.aspect.landscape') }}</option>
          <option value="square">{{ t('characterImagesPanel.generate.aspect.square') }}</option>
        </select>
        <select
          v-model.number="generateCount"
          class="field-select count-select"
          :disabled="generating || candidateUrls.length > 0"
          :title="t('characterImagesPanel.generate.countTitle')"
        >
          <option :value="1">{{ t('characterImagesPanel.generate.countOption', { count: 1 }) }}</option>
          <option :value="2">{{ t('characterImagesPanel.generate.countOption', { count: 2 }) }}</option>
          <option :value="3">{{ t('characterImagesPanel.generate.countOption', { count: 3 }) }}</option>
          <option :value="4">{{ t('characterImagesPanel.generate.countOption', { count: 4 }) }}</option>
        </select>
        <UiButton
          variant="primary"
          :loading="generating"
          :disabled="committing || !generatePrompt.trim() || candidateUrls.length > 0"
          @click="handleGenerate"
        >{{ generating ? t('characterImagesPanel.generate.generating') : t('characterImagesPanel.generate.action') }}</UiButton>
      </div>

    </div>

    <div v-if="errorMsg" class="images-error">{{ errorMsg }}</div>

    <!-- 候選 modal：Teleport 到 body 才不會被側邊欄的窄版型擠到。
         背景點擊刻意不關閉（會搞丟剛生成的圖）；關閉動作走明確按鈕或 ESC。 -->
    <Teleport to="body">
      <div v-if="candidateUrls.length > 0" class="candidate-modal-backdrop">
        <div class="candidate-modal" role="dialog" :aria-label="t('characterImagesPanel.candidates.ariaLabel')">
          <div class="candidate-modal-header">
            <div class="candidate-modal-title">
              {{ t('characterImagesPanel.candidates.title') }}
              <span class="candidate-modal-count">
                {{ candidateSummary }}
              </span>
            </div>
            <div class="candidate-modal-hint">
              {{ t('characterImagesPanel.candidates.hintPrefix') }}
              <b>{{ t('characterImagesPanel.candidates.targets.stage') }}</b>{{ t('characterImagesPanel.candidates.hintStageSuffix') }}
              → <b>{{ t('characterImagesPanel.candidates.targets.album') }}</b>{{ t('characterImagesPanel.candidates.hintAlbumSuffix') }}
              → <b>{{ t('characterImagesPanel.candidates.targets.discard') }}</b>{{ t('characterImagesPanel.candidates.hintDiscardSuffix') }}
              {{ t('characterImagesPanel.candidates.hintTail') }}
            </div>
            <div class="candidate-bulk-actions">
              <button class="chip-btn" :disabled="committing" @click="setAllTargets('stage')">{{ t('characterImagesPanel.candidates.bulkStage') }}</button>
              <button class="chip-btn" :disabled="committing" @click="setAllTargets('album')">{{ t('characterImagesPanel.candidates.bulkAlbum') }}</button>
              <button class="chip-btn" :disabled="committing" @click="setAllTargets('discard')">{{ t('characterImagesPanel.candidates.bulkDiscard') }}</button>
            </div>
          </div>

          <div class="candidate-modal-body">
            <div class="candidate-modal-grid">
              <div
                v-for="url in candidateUrls"
                :key="url"
                :class="[
                  'candidate-tile',
                  `target-${candidateTargets.get(url) ?? 'discard'}`,
                ]"
                @click="cycleCandidate(url)"
              >
                <img :src="url" :alt="t('characterImagesPanel.candidates.imageAlt')" />
                <span class="candidate-target-badge">
                  {{ candidateBadgeLabel(candidateTargets.get(url) ?? 'discard') }}
                </span>
              </div>
            </div>
          </div>

          <div class="candidate-modal-actions">
            <UiButton
              :disabled="committing"
              @click="discardAllCandidates"
            >{{ t('characterImagesPanel.candidates.discardAndClose') }}</UiButton>
            <UiButton
              variant="primary"
              :loading="committing"
              :disabled="commitCount === 0"
              @click="commitSelected"
            >{{ commitButtonLabel }}</UiButton>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.images-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0;
}

.images-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.images-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.images-empty {
  padding: 14px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.images-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(90px, 1fr));
  gap: 8px;
}

.image-tile {
  position: relative;
  aspect-ratio: 3 / 4;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
}

.image-tile img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.image-actions {
  position: absolute;
  inset: auto 0 0 0;
  display: flex;
  justify-content: space-between;
  gap: 2px;
  padding: 4px;
  background: linear-gradient(to top, rgba(0, 0, 0, 0.65), transparent);
  opacity: 0;
  transition: opacity 0.2s;
}

.image-tile:hover .image-actions {
  opacity: 1;
}

@media (hover: none) {
  .image-actions { opacity: 0.85; }
}

.tile-btn {
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.15);
  color: white;
  font-size: 11px;
  cursor: pointer;
}

.tile-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.tile-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.28);
}

.tile-btn-danger:hover:not(:disabled) {
  background: rgba(231, 76, 60, 0.5);
}

.primary-badge {
  position: absolute;
  top: 4px;
  left: 4px;
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(183, 93, 63, 0.85);
  color: white;
  font-weight: 600;
}

.upload-btn {
  display: block;
  padding: 10px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
  transition: background 0.2s;
}

.upload-btn:hover {
  background: rgba(255, 255, 255, 0.06);
}

.upload-btn input {
  display: none;
}

.upload-btn.disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.images-error {
  padding: 6px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}

.generate-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(183, 93, 63, 0.06);
  border: 1px solid rgba(183, 93, 63, 0.25);
  border-radius: 6px;
}

.generate-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-primary-light);
  letter-spacing: 0.5px;
}

.generate-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.generate-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 6px;
  align-items: center;
}

.count-select {
  min-width: 70px;
}

/* --- 候選 modal（Teleport 到 body） --- */
.candidate-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1200;
  background: rgba(0, 0, 0, 0.75);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  /* 避免 modal 被 sidebar 的 transform / overflow 裁切 */
}

.candidate-modal {
  width: min(1200px, 100%);
  max-height: calc(100vh - 48px);
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.55);
  overflow: hidden;
}

.candidate-modal-header {
  padding: 16px 20px 12px;
  border-bottom: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.candidate-modal-title {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 16px;
  font-weight: 600;
  color: var(--color-primary-light);
}

.candidate-modal-count {
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-secondary);
  letter-spacing: 0.3px;
}

.candidate-modal-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.candidate-modal-body {
  padding: 20px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}

.candidate-modal-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 14px;
}

.candidate-modal-actions {
  display: flex;
  gap: 10px;
  justify-content: flex-end;
  padding: 14px 20px;
  border-top: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.2);
}

@media (max-width: 640px) {
  .candidate-modal-backdrop { padding: 0; }
  .candidate-modal {
    width: 100%;
    max-height: 100vh;
    border-radius: 0;
    border: none;
  }
  .candidate-modal-grid {
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 8px;
  }
}

.candidate-tile {
  position: relative;
  aspect-ratio: 3 / 4;
  border-radius: 6px;
  overflow: hidden;
  border: 2px solid var(--color-border);
  background: var(--color-surface);
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s, box-shadow 0.15s;
}

.candidate-tile:hover {
  transform: scale(1.02);
}

.candidate-tile.target-stage {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 2px rgba(183, 93, 63, 0.3);
}

.candidate-tile.target-album {
  border-color: #6aa9d8;
  box-shadow: 0 0 0 2px rgba(106, 169, 216, 0.3);
}

.candidate-tile img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  /* Dim discards to keep the picks visually obvious. */
  opacity: 0.55;
  transition: opacity 0.15s;
}

.candidate-tile.target-stage img,
.candidate-tile.target-album img {
  opacity: 1;
}

.candidate-target-badge {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 3px 8px;
  border-radius: 10px;
  background: rgba(0, 0, 0, 0.62);
  color: white;
  font-size: 11px;
  font-weight: 600;
  line-height: 1;
  letter-spacing: 0.3px;
}

.candidate-tile.target-stage .candidate-target-badge {
  background: var(--color-primary);
}

.candidate-tile.target-album .candidate-target-badge {
  background: #4c82ae;
}

.candidate-bulk-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 6px;
}

.chip-btn {
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}

.chip-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}

.chip-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* 共用欄位樣式在 global style.css */

</style>
