<script setup lang="ts">
import { computed, nextTick, ref, watch, onBeforeUnmount, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { ZoomInOutlined } from '@ant-design/icons-vue'
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
import {
  billingRefusalKind,
  refreshQuotedPrices,
} from '@/utils/api/billingRefusal'
import { UiButton, UiImage, UiLightbox } from '@/components/ui'
import type { LightboxItem } from '@/components/ui'
import { syncZoomAfterUrlsChange } from '@/utils/imageZoomSync'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import InsufficientCreditsNotice from '@/components/InsufficientCreditsNotice.vue'
import {
  refreshCloudCreditsAfterAction,
  useCloudCredits,
} from '@/composables/useCloudCredits'
import {
  ACTION_IMAGE_PORTRAIT,
  useActionPricing,
} from '@/composables/useActionPricing'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useRuntimeLimits } from '@/composables/useRuntimeLimits'

const props = withDefaults(defineProps<{
  character: Character
  /**
   * Operator-grade wording (which image channel is used, LoRA vs OpenAI
   * behaviour). Defaulted to `true` — explicitly, because Vue would
   * otherwise cast an absent Boolean prop to `false` — so the self-host
   * panel stays byte-identical; the hosted player surface passes `false`
   * and gets copy that says what happens rather than how it is wired
   * (plan U1-A).
   */
  showTechnicalHints?: boolean
}>(), {
  showTechnicalHints: true,
})

const emit = defineEmits<{
  updated: [char: Character]
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const generateHint = computed(() => (
  props.showTechnicalHints
    ? t('characterImagesPanel.generate.hint')
    : t('characterImagesPanel.generate.hintCloud')
))

const uploading = ref(false)
const busyUrl = ref<string | null>(null)
const errorMsg = ref<string | null>(null)
// Generation refused for lack of credits: shown as the shared notice card
// rather than in the generic error line, so the "nothing was charged" promise
// and the top-up CTA travel with it.
const creditsExhausted = ref(false)
// Price of a refusal that came from the local pre-check (plan AP2); null for
// the server's 402, which is worded without a number.
const creditsRequiredCr = ref<number | null>(null)

const cloudCredits = useCloudCredits()
const actionPricing = useActionPricing()
const runtimeLimits = useRuntimeLimits()

/**
 * This plan does not include generating new pictures (U2: hosted
 * `album_generation_enabled = false`, a per-tier switch for plans that do not
 * carry the generation cost). Uploading is untouched — the server only gates
 * the *generated* path, so taking the upload button away would remove
 * something the player still has.
 *
 * True only when a loaded hosted snapshot says so: unknown, unreadable and
 * self-host all read as enabled, so nothing here can withhold a feature on
 * a failed request.
 */
const albumGenerationBlocked = computed(
  () => !runtimeLimits.albumGenerationEnabled.value,
)

onMounted(() => {
  void runtimeLimits.ensureLoaded()
})

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

// ---------------------------------------------------------------- 放大檢視
//
// 這個面板本來**完全看不到大圖**（LB5）：舞台縮圖只有 90px、格子上只有排序／
// 封存／刪除；候選圖整格的點擊語意被「循環挑選」佔走，而挑圖正是最需要看清楚
// 的時刻。這裡補上的是一個從來沒有的能力，不是換掉既有行為。
//
// 兩處各自一份集合，刻意**不合併**：舞台圖是「已經在用的」，候選圖是「還沒
// 決定去留的」，混成一份會讓左右鍵從候選圖滑進舞台圖，等於在謊報集合的邊界。

const stageZoomOpen = ref(false)
const stageZoomIndex = ref(0)
const candidateZoomOpen = ref(false)
const candidateZoomIndex = ref(0)

const stageLightboxItems = computed<LightboxItem[]>(() =>
  props.character.image_urls.map((url, index) => ({
    url,
    alt: t('characterImagesPanel.imageAlt', {
      name: props.character.name,
      index: index + 1,
    }),
  })),
)

const candidateLightboxItems = computed<LightboxItem[]>(() =>
  candidateUrls.value.map(url => ({
    url,
    alt: t('characterImagesPanel.candidates.imageAlt'),
  })),
)

function openStageZoom(at: number) {
  stageZoomIndex.value = at
  stageZoomOpen.value = true
}

function openCandidateZoom(at: number) {
  candidateZoomIndex.value = at
  candidateZoomOpen.value = true
}

/**
 * 舞台清單在浮窗開著時被換掉：排序讓同一張圖換位置（跟著走），刪除／封存讓它
 * 整筆消失（關窗）。判斷本身在 `syncZoomAfterUrlsChange`——索引是位置不是身分，
 * 不跟就會無聲地變成在看另一張圖。
 */
watch(
  () => props.character.image_urls,
  (after, before) => {
    const result = syncZoomAfterUrlsChange({
      open: stageZoomOpen.value,
      index: stageZoomIndex.value,
      before,
      after,
    })
    if (result.outcome === 'close') stageZoomOpen.value = false
    else if (result.outcome === 'move') stageZoomIndex.value = result.index
  },
)

/** 候選圖被 commit／捨棄掉（集合變空）時，蓋在上面的浮窗也要跟著收。 */
watch(candidateUrls, (after, before) => {
  const result = syncZoomAfterUrlsChange({
    open: candidateZoomOpen.value,
    index: candidateZoomIndex.value,
    before,
    after,
  })
  if (result.outcome === 'close') candidateZoomOpen.value = false
  else if (result.outcome === 'move') candidateZoomIndex.value = result.index
})

/**
 * 換角色：整份清單都是別人的，索引指向的位置在新清單裡是另一個人的圖。
 * 上面兩條 watch 多半也會抓到，但那依賴「新舊清單剛好沒有同一個 URL」——
 * 換人這件事本身就足以關窗，不必繞過內容比對。
 */
watch(() => props.character.id, () => {
  stageZoomOpen.value = false
  stageZoomIndex.value = 0
  candidateZoomOpen.value = false
  candidateZoomIndex.value = 0
})

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
    errorMsg.value = props.showTechnicalHints
      ? t('characterImagesPanel.errors.promptRequired')
      : t('characterImagesPanel.errors.promptRequiredCloud')
    return
  }
  // AP2 pre-check: the price is published and the balance is already on
  // screen, so an unaffordable press is answered instantly instead of after
  // a round trip that ends in a 402.
  const shortfall = actionPricing.shortfallFor(ACTION_IMAGE_PORTRAIT, {
    total: cloudCredits.total.value,
    known: cloudCredits.hasBalance.value,
    stale: cloudCredits.stale.value,
  })
  if (shortfall !== null) {
    creditsRequiredCr.value = shortfall
    creditsExhausted.value = true
    return
  }
  generating.value = true
  errorMsg.value = null
  creditsExhausted.value = false
  creditsRequiredCr.value = null
  try {
    const res = await generatePortraitCandidates(
      props.character.id, positive, generateAspect.value, generateCount.value,
    )
    candidateUrls.value = res.candidates
    // Candidates are back, so the charge already happened — settle the badge.
    refreshCloudCreditsAfterAction()
    // Default: every candidate pre-selected for stage — saves a click
    // when user wants to keep them all. Click cycles into album
    // or discard per-tile.
    const fresh = new Map<string, CandidateTarget>()
    for (const url of res.candidates) fresh.set(url, 'stage')
    candidateTargets.value = fresh
  } catch (err) {
    switch (billingRefusalKind(err)) {
      case 'insufficient_credits':
        creditsExhausted.value = true
        break
      case 'price_changed':
        // The batch is action-priced now, so this refusal is reachable here:
        // the published price moved between the hint on screen and the press.
        // Nothing was charged, so the only useful reply is the new number plus
        // "send it again" — refreshing first is what makes the hint above the
        // button agree with the retry.
        await refreshQuotedPrices()
        errorMsg.value = t('credits.price.changed')
        break
      default:
        errorMsg.value = extractError(err) ?? t('characterImagesPanel.errors.generateFailed')
    }
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
//
// 這支監聽是 bubble 階段（`addEventListener` 預設）。放大浮窗開著時 Esc 到不了
// 這裡——`UiLightbox` 以 **capture 階段**掛在 `window` 上（事件路徑的第一站）
// 並對 Esc 呼叫 `stopPropagation()`。這條很要緊：這裡的 Esc 是「全部捨棄」，
// 會一次抹掉剛花點數生出來的整批候選圖，而玩家在放大檢視時按 Esc 的意思只是
// 「關掉這張大圖」。**不要在這裡補手寫讓位守衛**——攔截責任在浮窗，宿主自己
// 記得讓位的那套模式漏一個就是一次不可復原的破壞，而且不會有測試變紅。
function onKeydown(event: KeyboardEvent) {
  if (event.key !== 'Escape') return
  if (candidateUrls.value.length === 0) return
  if (committing.value) return
  event.preventDefault()
  discardAllCandidates()
}

/**
 * 候選 modal 的背景捲動鎖。
 *
 * **要說兩次。** `UiLightbox` 有自己的一套鎖（模組層級計數 ＋ 記住「上鎖那一
 * 刻的 overflow」），它解鎖時還原的正是那個值——而候選圖上的浮窗，上鎖那一刻
 * 看到的就是這個 modal 寫下的 `hidden`。子元件的 watcher 與 unmount hook 一律
 * 排在本元件之後，所以「候選圖連同浮窗一起收掉」時，只寫一次會被浮窗的還原
 * 蓋回 `hidden`，留下一個沒有任何 modal 卻捲不動的頁面。等它做完再說一次。
 */
function applyCandidateScrollLock(locked: boolean) {
  if (typeof document === 'undefined') return
  document.body.style.overflow = locked ? 'hidden' : ''
}

watch(candidateUrls, (urls) => {
  const locked = urls.length > 0
  applyCandidateScrollLock(locked)
  void nextTick(() => applyCandidateScrollLock(locked))
})

if (typeof window !== 'undefined') {
  window.addEventListener('keydown', onKeydown)
  onBeforeUnmount(() => {
    window.removeEventListener('keydown', onKeydown)
    applyCandidateScrollLock(false)
    void nextTick(() => applyCandidateScrollLock(false))
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
        <!-- 90px 縮圖以前完全點不開（LB5）。整張縮圖就是放大入口；排序／封存／
             刪除留在格子上，而且是這顆按鈕的**兄弟**不是子孫——包進去的話那四顆
             按下去會一路冒泡成「開浮窗」。 -->
        <button
          type="button"
          class="image-zoom-button"
          :title="t('common.actions.zoom')"
          :aria-label="t('common.actions.zoomAria', {
            name: t('characterImagesPanel.imageAlt', { name: character.name, index: index + 1 }),
          })"
          @click="openStageZoom(index)"
        >
          <UiImage
            :src="url"
            :alt="t('characterImagesPanel.imageAlt', { name: character.name, index: index + 1 })"
            variant="thumb"
            sizes="90px"
          />
        </button>
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
        {{ generateHint }}
      </div>
      <!-- 此方案沒有 AI 生成：說在按下去之前。只有在確定讀到 hosted
           快照且明說關閉時才輸出，自架與讀不到時零節點。上傳不受影響。 -->
      <p
        v-if="albumGenerationBlocked"
        class="generate-disabled-note"
        role="status"
      >{{ t('characterImagesPanel.generate.disabledNotice') }}</p>
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
          :disabled="
            committing
              || !generatePrompt.trim()
              || candidateUrls.length > 0
              || albumGenerationBlocked
          "
          @click="handleGenerate"
        >{{ generating ? t('characterImagesPanel.generate.generating') : t('characterImagesPanel.generate.action') }}</UiButton>
      </div>
      <!-- 明碼標價：按下去要花多少，按之前就看得到。放在生成列外面是因為
           那一列是三欄 grid；查不到價格時本元件不輸出任何節點。 -->
      <ActionPriceHint
        class="generate-price-hint"
        :action-key="ACTION_IMAGE_PORTRAIT"
        tooltip-key="credits.price.imageTooltip"
        variant="chip"
      />

    </div>

    <div v-if="errorMsg" class="images-error">{{ errorMsg }}</div>
    <InsufficientCreditsNotice
      v-if="creditsExhausted"
      class="images-credits-notice"
      :required-cr="creditsRequiredCr"
    />

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
                v-for="(url, index) in candidateUrls"
                :key="url"
                :class="[
                  'candidate-tile',
                  `target-${candidateTargets.get(url) ?? 'discard'}`,
                ]"
                @click="cycleCandidate(url)"
              >
                <UiImage
                  :src="url"
                  :alt="t('characterImagesPanel.candidates.imageAlt')"
                  variant="thumb"
                  sizes="(max-width: 640px) 140px, 260px"
                />
                <!-- 整格的點擊是「循環挑選 採用／捨棄」，那是挑圖流程的主要
                     操作，絕不能被放大搶走——所以放大另給一顆明確的鍵，並
                     `.stop` 掉冒泡。挑圖正是最需要看清楚的時刻，所以它不藏在
                     hover 後面（觸控裝置根本觸發不了 hover）。 -->
                <button
                  type="button"
                  class="candidate-zoom-btn"
                  :title="t('common.actions.zoom')"
                  :aria-label="t('common.actions.zoomAria', {
                    name: `${t('characterImagesPanel.candidates.imageAlt')} ${index + 1}`,
                  })"
                  @click.stop="openCandidateZoom(index)"
                >
                  <ZoomInOutlined aria-hidden="true" />
                </button>
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

    <!-- 兩份集合各一個浮窗（計畫 §3.3：排序／封存／刪除／挑選都留在縮圖格子
         上，primitive 不開 actions slot）。兩者都在候選 modal 之外宣告：候選圖
         被 commit／捨棄的瞬間，浮窗要由上面那條 watch 明確收掉，而不是靠自己
         被拆掉。元件自己 Teleport 到 body、z-index 1500，疊得過本面板的 1200，
         也不會被 sidebar 的 transform 困住。 -->
    <UiLightbox
      v-model:index="stageZoomIndex"
      :visible="stageZoomOpen"
      :items="stageLightboxItems"
      :label="t('characterImagesPanel.title')"
      @close="stageZoomOpen = false"
    />
    <UiLightbox
      v-model:index="candidateZoomIndex"
      :visible="candidateZoomOpen"
      :items="candidateLightboxItems"
      :label="t('characterImagesPanel.candidates.title')"
      @close="candidateZoomOpen = false"
    />
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

/* 縮圖外面新包的按鈕（LB5）。按鈕自帶的 padding / border / 系統底色要清掉，
   否則格子會多出一圈邊；:focus-visible 內縮，才不會被 .image-tile 的
   overflow: hidden 裁掉。 */
.image-zoom-button {
  display: block;
  width: 100%;
  height: 100%;
  /* 高度說兩次：`.image-tile` 的高度來自它自己的 aspect-ratio，而百分比高度要
     父層高度是「確定的」才解得出來。自己也帶一份比例，兩條路得到同一個box。 */
  aspect-ratio: 3 / 4;
  padding: 0;
  border: none;
  background: none;
  cursor: zoom-in;
}

.image-zoom-button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
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

.images-credits-notice {
  margin-top: 8px;
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

.generate-disabled-note {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid rgba(var(--color-spark-rgb), 0.26);
  border-radius: 6px;
  background: rgba(var(--color-spark-rgb), 0.07);
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.generate-row {
  display: grid;
  grid-template-columns: 1fr auto auto;
  gap: 6px;
  align-items: center;
}

.generate-price-hint {
  align-self: flex-end;
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

/* 放大鍵浮在候選圖左上（徽章佔了右上）。44px 是觸控目標的下限——這顆鍵和整格
   的「循環挑選」擠在同一格裡，按不準的代價是誤改了去留。 */
.candidate-zoom-btn {
  position: absolute;
  top: 6px;
  left: 6px;
  width: 44px;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid rgba(255, 255, 255, 0.28);
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.62);
  color: #fff;
  font-size: 17px;
  line-height: 1;
  cursor: zoom-in;
  opacity: 0.88;
  transition: opacity 0.15s, background 0.15s;
}

.candidate-zoom-btn:hover {
  opacity: 1;
  background: rgba(0, 0, 0, 0.82);
}

.candidate-zoom-btn:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: 2px;
}

@media (max-width: 640px) {
  .candidate-zoom-btn {
    width: 40px;
    height: 40px;
    font-size: 16px;
  }
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
