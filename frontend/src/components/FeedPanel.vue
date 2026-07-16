<script setup lang="ts">
/**
 * LumeGram 動態列表（Instagram 風）。
 *
 * 兩種模式由 ``characterId`` 切換：
 *
 * - ``null`` ── 全局牆：拉 ``listGlobalFeed``，每張卡帶角色頭像+名字，
 *   點頭像 emit ``select-character`` 讓 overlay 切到 filter 模式。
 *   character meta 由 ``listCharacters()`` 一次性 fetch + lazy 補漏。
 * - ``string`` ── filter 模式：拉 ``listCharacterFeed``（既有流程），
 *   FeedCard 不再渲染角色 header（overlay 標題已經顯示角色名）；
 *   只有此模式才開啟「手動為角色發一篇」操作。
 *
 * SSE ``feed_post``：
 *   - 全局：任何角色的新貼文都觸發 reload
 *   - filter：只對符合的角色 reload，避免 A 看 B 動態時被 B 的事件干擾
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import FeedCard from './FeedCard.vue'
import type { Character } from '@/types/character'
import type { FeedPost, FeedReactionState } from '@/types/feed'
import { getCharacter, listCharacters } from '@/utils/api/characters'
import { UiButton } from '@/components/ui'

const { t } = useI18n()
import {
  createManualFeedPost,
  listCharacterFeed,
  listGlobalFeed,
  markFeedReactionsSeen,
} from '@/utils/api/feed'
import {
  type FeedEventStreamHandle,
  type FeedPostEvent,
  connectFeedEvents,
} from '@/utils/feedEvents'

const props = defineProps<{
  characterId: string | null
  characterName?: string | null
}>()

const emit = defineEmits<{
  (e: 'select-character', characterId: string): void
}>()

const items = ref<FeedPost[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(false)
const nextBefore = ref<string | null>(null)
const errorMsg = ref<string | null>(null)
const stream = ref<FeedEventStreamHandle | null>(null)
const composeOpen = ref(false)
const composeText = ref('')
const composeBusy = ref(false)
// 全局模式才用 — id → Character meta，餵 FeedCard 頭像/名字。
const characterMap = ref<Map<string, Character>>(new Map())

const PAGE_SIZE = 20
const COMPOSE_MAX = 4000

const isGlobal = computed(() => props.characterId === null)

function rememberCharacters(list: Character[]) {
  if (list.length === 0) return
  const next = new Map(characterMap.value)
  for (const c of list) next.set(c.id, c)
  characterMap.value = next
}

async function ensureCharacterLoaded(characterId: string) {
  if (characterMap.value.has(characterId)) return
  try {
    const c = await getCharacter(characterId)
    rememberCharacters([c])
  } catch {
    // 角色被刪了或抓不到都 fail-soft；卡片會 fallback 用 id 當名字。
  }
}

function nameOf(characterId: string): string {
  return characterMap.value.get(characterId)?.name ?? characterId
}

function avatarOf(characterId: string): string | null {
  return characterMap.value.get(characterId)?.image_urls?.[0] ?? null
}

async function backfillMissingCharacterMeta(posts: FeedPost[]) {
  const missing = [...new Set(
    posts
      .map(p => p.character_id)
      .filter(id => !characterMap.value.has(id)),
  )]
  if (missing.length === 0) return
  await Promise.all(missing.map(ensureCharacterLoaded))
}

async function reload() {
  loading.value = true
  errorMsg.value = null
  const filterId = props.characterId
  try {
    if (filterId === null) {
      // 全局：feed + character map 並行拉以縮短首屏。
      const [feed, chars] = await Promise.all([
        listGlobalFeed({ limit: PAGE_SIZE }),
        characterMap.value.size === 0
          ? listCharacters()
          : Promise.resolve([] as Character[]),
      ])
      rememberCharacters(chars)
      items.value = feed.items
      hasMore.value = feed.has_more
      nextBefore.value = feed.next_before
      await backfillMissingCharacterMeta(feed.items)
    } else {
      const res = await listCharacterFeed(filterId, { limit: PAGE_SIZE })
      items.value = res.items
      hasMore.value = res.has_more
      nextBefore.value = res.next_before
      // filter 模式：fire-and-forget 把 unseen 反應折成記憶。
      markFeedReactionsSeen(filterId).catch(() => {})
    }
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('feed.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

async function loadMore() {
  if (!hasMore.value || !nextBefore.value) return
  loadingMore.value = true
  errorMsg.value = null
  const filterId = props.characterId
  try {
    const res = filterId === null
      ? await listGlobalFeed({
          limit: PAGE_SIZE, before: nextBefore.value,
        })
      : await listCharacterFeed(filterId, {
          limit: PAGE_SIZE, before: nextBefore.value,
        })
    items.value = [...items.value, ...res.items]
    hasMore.value = res.has_more
    nextBefore.value = res.next_before
    if (filterId === null) {
      await backfillMissingCharacterMeta(res.items)
    }
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('feed.errors.loadMoreFailed')
  } finally {
    loadingMore.value = false
  }
}

function applyReactionState(state: FeedReactionState) {
  const idx = items.value.findIndex(p => p.id === state.post_id)
  if (idx === -1) return
  const current = items.value[idx]
  items.value[idx] = {
    ...current,
    liked: state.liked,
    reactions: { ...current.reactions, likes: state.likes },
  }
}

function applyCommentCount(payload: { post_id: string; comments: number }) {
  const idx = items.value.findIndex(p => p.id === payload.post_id)
  if (idx === -1) return
  const current = items.value[idx]
  items.value[idx] = {
    ...current,
    reactions: { ...current.reactions, comments: payload.comments },
  }
}

function showReactionError(message: string) {
  errorMsg.value = message
}

function toggleCompose() {
  composeOpen.value = !composeOpen.value
  if (!composeOpen.value) {
    composeText.value = ''
  }
}

async function submitManualPost() {
  if (!props.characterId) return
  const text = composeText.value.trim()
  if (!text) return
  composeBusy.value = true
  errorMsg.value = null
  try {
    const post = await createManualFeedPost(props.characterId, {
      content_text: text,
    })
    // 樂觀 prepend；SSE handler 用 id dedup。
    if (!items.value.some(p => p.id === post.id)) {
      items.value = [post, ...items.value]
    }
    composeText.value = ''
    composeOpen.value = false
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('feed.errors.composeFailed')
  } finally {
    composeBusy.value = false
  }
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}

function handleFeedEvent(evt: FeedPostEvent) {
  if (items.value.some(p => p.id === evt.post_id)) return
  if (props.characterId !== null && evt.character_id !== props.characterId) {
    // filter 模式：不是當前角色的事件就忽略，避免被別人的貼文打斷瀏覽
    return
  }
  reload()
}

function handleSelectCharacter(characterId: string) {
  emit('select-character', characterId)
}

watch(() => props.characterId, () => {
  reload()
}, { immediate: true })

// One stream per panel mount；mode 切換時 handler 用 props.characterId
// 判斷，所以不需要重連。
stream.value = connectFeedEvents(handleFeedEvent)

onBeforeUnmount(() => {
  stream.value?.close()
  stream.value = null
})

defineExpose({ reload })
</script>

<template>
  <div class="feed-panel">
    <div v-if="!isGlobal" class="feed-header">
      <p class="feed-hint">
        {{ t('feed.panel.hint') }}
        <strong>{{ items.length > 0 ? t('feed.panel.hintPerCharacter') : t('feed.panel.hintDefault') }}</strong>
        {{ t('feed.panel.hintTail') }}
      </p>
      <div class="feed-compose-toggle">
        <UiButton
          size="sm"
          :disabled="composeBusy"
          @click="toggleCompose"
        >
          {{ composeOpen ? t('feed.panel.composeCancel') : t('feed.panel.composeOpen') }}
        </UiButton>
      </div>
      <div v-if="composeOpen" class="feed-compose">
        <textarea
          v-model="composeText"
          class="feed-compose-input"
          :maxlength="COMPOSE_MAX"
          rows="3"
          :placeholder="t('feed.panel.composePlaceholder')"
          :disabled="composeBusy"
        />
        <div class="feed-compose-actions">
          <span class="feed-compose-counter">
            {{ composeText.length }} / {{ COMPOSE_MAX }}
          </span>
          <UiButton
            variant="primary"
            size="sm"
            :loading="composeBusy"
            :disabled="composeText.trim().length === 0"
            @click="submitManualPost"
          >
            {{ composeBusy ? t('feed.panel.composeSubmitting') : t('feed.panel.composeSubmit') }}
          </UiButton>
        </div>
      </div>
    </div>

    <div v-if="loading" class="feed-empty">{{ t('feed.panel.loading') }}</div>
    <div v-else-if="items.length === 0" class="feed-empty">
      <template v-if="isGlobal">{{ t('feed.panel.emptyGlobal') }}</template>
      <template v-else>{{ t('feed.panel.emptyCharacter') }}</template>
    </div>
    <div v-else class="feed-list">
      <FeedCard
        v-for="post in items"
        :key="post.id"
        :post="post"
        :character-id="post.character_id"
        :character-name="
          isGlobal ? nameOf(post.character_id) : (characterName ?? null)
        "
        :character-avatar="isGlobal ? avatarOf(post.character_id) : null"
        :on-open-profile="isGlobal ? handleSelectCharacter : null"
        @reaction-changed="applyReactionState"
        @reaction-error="showReactionError"
        @comment-count-changed="applyCommentCount"
      />
      <div v-if="hasMore" class="feed-more">
        <UiButton
          size="sm"
          :loading="loadingMore"
          @click="loadMore"
        >
          {{ loadingMore ? t('feed.panel.loadMoreLoading') : t('feed.panel.loadMore') }}
        </UiButton>
      </div>
    </div>

    <div v-if="errorMsg" class="feed-error">{{ errorMsg }}</div>
  </div>
</template>

<style scoped>
.feed-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.feed-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  letter-spacing: 0.5px;
  margin: 0;
}

.feed-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.feed-empty {
  padding: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.feed-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.feed-more {
  display: flex;
  justify-content: center;
  padding: 4px 0;
}

.feed-error {
  padding: 8px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}

.feed-compose-toggle {
  display: flex;
  justify-content: flex-end;
}

.feed-compose {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}

.feed-compose-input {
  width: 100%;
  background: rgba(0, 0, 0, 0.2);
  border: 1px solid var(--color-border);
  color: var(--color-text);
  border-radius: 4px;
  padding: 6px 8px;
  font-size: 12px;
  font-family: inherit;
  resize: vertical;
  box-sizing: border-box;
}

.feed-compose-input:disabled {
  opacity: 0.5;
}

.feed-compose-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.feed-compose-counter {
  font-size: 11px;
  color: var(--color-text-secondary);
}

</style>
