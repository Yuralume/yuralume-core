<script setup lang="ts">
/**
 * 單張角色動態卡片。
 *
 * Instagram 風格：上方圖片（若有）、下方文字、底部反應計數 + 來源
 * tag。Phase A1 加上愛心 toggle，Phase A2 加上留言區（按下留言計數
 * 才展開、避免一次拉每張卡片的留言）。樂觀更新：點擊先 flip，
 * 失敗 rollback 並把錯誤往上層丟。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type {
  FeedComment,
  FeedPost,
  FeedReactionState,
} from '@/types/feed'
import {
  createFeedComment,
  deleteFeedComment,
  likeFeedPost,
  listFeedComments,
  unlikeFeedPost,
} from '@/utils/api/feed'
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { formatDate, formatRelativeTime } from '@/i18n/formatters'

const LOCAL_AUTHOR_ID = 'local'
const { timeZone } = useTimezone()

const props = defineProps<{
  post: FeedPost
  characterId?: string | null
  characterName?: string | null
  // 角色頭像 URL（取 character.image_urls[0]）。給就會渲染 IG 風的
  // 帳號 header；不給就只顯示名字或省略 header。全局牆會用、單角色
  // overlay 因為標題已經有角色名所以不用傳。
  characterAvatar?: string | null
  // 點頭像 / 帳號的回呼。給的話會把 character header 變成可點擊（cursor:
  // pointer + hover 樣式）；通常 GlobalFeedPage 會傳一個 router.push 進來。
  onOpenProfile?: ((characterId: string) => void) | null
}>()

const emit = defineEmits<{
  (e: 'reaction-changed', value: FeedReactionState): void
  (e: 'reaction-error', message: string): void
  (e: 'comment-count-changed', value: { post_id: string; comments: number }): void
}>()

const { t } = useI18n()
const { locale } = useLocale()
const { currentUser } = useAuth()

const headerName = computed(() =>
  (props.characterName ?? '').trim() || t('common.fallback.character'),
)
const headerInitial = computed(() => {
  const trimmed = (props.characterName ?? '').trim()
  return trimmed ? trimmed.slice(0, 1) : '?'
})
const headerClickable = computed(() =>
  Boolean(props.onOpenProfile && props.characterId),
)

function openProfile() {
  if (!props.onOpenProfile || !props.characterId) return
  props.onOpenProfile(props.characterId)
}

// like state mirrors prop, can drift optimistically.
const liked = ref(props.post.liked)
const likes = ref(props.post.reactions.likes)
const pending = ref(false)

// comment state — lazy: nothing fetched until expanded once.
const showComments = ref(false)
const comments = ref<FeedComment[]>([])
const commentsLoaded = ref(false)
const commentsLoading = ref(false)
const commentDraft = ref('')
const commentSubmitting = ref(false)
// Local override for the comment count so the badge reflects optimistic
// adds before the parent's reload propagates.
const commentCountOverride = ref<number | null>(null)
const commentCount = computed(() =>
  commentCountOverride.value ?? props.post.reactions.comments,
)

watch(
  () => [props.post.liked, props.post.reactions.likes] as const,
  ([nextLiked, nextCount]) => {
    liked.value = nextLiked
    likes.value = nextCount
  },
)

watch(
  () => props.post.reactions.comments,
  () => {
    commentCountOverride.value = null
  },
)

async function toggleLike() {
  if (pending.value) return
  const wasLiked = liked.value
  const wasCount = likes.value
  liked.value = !wasLiked
  likes.value = wasLiked ? Math.max(0, wasCount - 1) : wasCount + 1
  pending.value = true
  try {
    const state = wasLiked
      ? await unlikeFeedPost(props.post.id)
      : await likeFeedPost(props.post.id)
    liked.value = state.liked
    likes.value = state.likes
    emit('reaction-changed', state)
  } catch (err) {
    liked.value = wasLiked
    likes.value = wasCount
    emit('reaction-error', extractError(err) ?? t('feed.errors.reactFailed'))
  } finally {
    pending.value = false
  }
}

async function toggleComments() {
  showComments.value = !showComments.value
  if (showComments.value && !commentsLoaded.value) {
    await refreshComments()
  }
}

async function refreshComments() {
  commentsLoading.value = true
  try {
    const res = await listFeedComments(props.post.id, { limit: 100 })
    comments.value = res.items
    commentsLoaded.value = true
  } catch (err) {
    emit('reaction-error', extractError(err) ?? t('feed.errors.loadCommentsFailed'))
  } finally {
    commentsLoading.value = false
  }
}

async function submitComment() {
  const text = commentDraft.value.trim()
  if (!text || commentSubmitting.value) return
  commentSubmitting.value = true
  try {
    const created = await createFeedComment(props.post.id, text)
    comments.value = [created, ...comments.value]
    commentDraft.value = ''
    const next = commentCount.value + 1
    commentCountOverride.value = next
    emit('comment-count-changed', {
      post_id: props.post.id, comments: next,
    })
  } catch (err) {
    emit('reaction-error', extractError(err) ?? t('feed.errors.commentFailed'))
  } finally {
    commentSubmitting.value = false
  }
}

async function removeComment(commentId: string) {
  try {
    await deleteFeedComment(commentId)
    comments.value = comments.value.filter(c => c.id !== commentId)
    const next = Math.max(0, commentCount.value - 1)
    commentCountOverride.value = next
    emit('comment-count-changed', {
      post_id: props.post.id, comments: next,
    })
  } catch (err) {
    emit('reaction-error', extractError(err) ?? t('feed.errors.deleteCommentFailed'))
  }
}

function isOwnComment(comment: FeedComment): boolean {
  if (comment.author_id === LOCAL_AUTHOR_ID) return true
  return Boolean(currentUser.value?.id && comment.author_id === currentUser.value.id)
}

function formatAuthor(comment: FeedComment): string {
  const displayName = comment.author_display_name?.trim()
  if (displayName) {
    // Substitute a localized label for the seeded `操作者` placeholder
    // instead of leaking the raw zh-TW sentinel to en/ja operators.
    return comment.author_display_name_is_placeholder
      ? t('common.operatorPlaceholder')
      : displayName
  }
  const authorId = comment.author_id
  if (authorId === LOCAL_AUTHOR_ID) return t('chat.bubble.you')
  if (currentUser.value?.id && authorId === currentUser.value.id) {
    if (currentUser.value.display_name_is_placeholder) {
      return t('common.operatorPlaceholder')
    }
    return currentUser.value.display_name?.trim() || t('chat.bubble.you')
  }
  if (props.characterId && authorId === props.characterId) {
    return props.characterName?.trim() || t('common.fallback.character')
  }
  return authorId
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}

const sourceLabel = computed(() => {
  switch (props.post.source.kind) {
    case 'schedule':
      return t('feed.card.source.schedule')
    case 'beat':
      return t('feed.card.source.beat')
    case 'memory':
      return t('feed.card.source.memory')
    case 'world_event':
      return t('feed.card.source.worldEvent')
    case 'silence':
      return t('feed.card.source.silence')
    case 'state_shift':
      return t('feed.card.source.stateShift')
    case 'manual':
      return t('feed.card.source.manual')
    default:
      return props.post.source.kind
  }
})

const kindLabel = computed(() => {
  switch (props.post.kind) {
    case 'mood':
      return t('feed.card.kind.mood')
    case 'reflection':
      return t('feed.card.kind.reflection')
    case 'work':
      return t('feed.card.kind.work')
    case 'scene_beat':
      return t('feed.card.kind.sceneBeat')
    case 'external':
      return t('feed.card.kind.external')
    case 'daily':
      return t('feed.card.kind.daily')
    default:
      return props.post.kind
  }
})

function formatRelative(iso: string): string {
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return iso
  const diffMs = Math.abs(Date.now() - ts)
  if (diffMs >= 7 * 24 * 60 * 60 * 1000) {
    return formatDate(iso, locale.value, timeZone.value)
  }
  return formatRelativeTime(iso, locale.value)
}
</script>

<template>
  <article class="feed-card">
    <!-- IG 風 character header — 只在 characterName 有給時渲染。全局牆
         會把 onOpenProfile 傳進來讓整列可點，點擊跳該角色的個人 stage。 -->
    <header
      v-if="characterName"
      class="feed-card-author"
      :class="{ 'is-clickable': headerClickable }"
      :role="headerClickable ? 'button' : undefined"
      :tabindex="headerClickable ? 0 : undefined"
      @click="openProfile"
      @keydown.enter.prevent="openProfile"
      @keydown.space.prevent="openProfile"
    >
      <span class="feed-card-avatar">
        <img
          v-if="characterAvatar"
          :src="characterAvatar"
          :alt="headerName"
          loading="lazy"
        />
        <span v-else class="feed-card-avatar-fallback">
          {{ headerInitial }}
        </span>
      </span>
      <span class="feed-card-author-name">{{ headerName }}</span>
    </header>
    <!-- Prefer video when present; falls back to image, then text-only.
         The two render paths are mutually exclusive on the same post —
         the backend never sets both simultaneously today. -->
    <div
      v-if="post.video_url"
      class="feed-card-video-wrap"
    >
      <video
        :src="post.video_url"
        class="feed-card-video"
        controls
        playsinline
        loop
        muted
        preload="metadata"
      />
    </div>
    <a
      v-else-if="post.image_url"
      :href="post.image_url"
      target="_blank"
      rel="noopener noreferrer"
      class="feed-card-image-link"
    >
      <img
        :src="post.image_url"
        :alt="post.content_text.slice(0, 40)"
        loading="lazy"
      />
    </a>
    <div class="feed-card-body">
      <div class="feed-card-meta">
        <span class="feed-card-kind">{{ kindLabel }}</span>
        <span class="feed-card-sep">·</span>
        <span class="feed-card-source">{{ t('feed.card.sourceLabel', { source: sourceLabel }) }}</span>
        <span class="feed-card-spacer" />
        <time class="feed-card-time" :title="post.created_at">
          {{ formatRelative(post.created_at) }}
        </time>
      </div>
      <p class="feed-card-text">{{ post.content_text }}</p>
      <div class="feed-card-reactions">
        <button
          type="button"
          class="feed-card-like"
          :class="{ 'is-liked': liked, 'is-pending': pending }"
          :disabled="pending"
          :title="liked ? t('feed.card.unreact') : t('feed.card.reactLike')"
          :aria-pressed="liked"
          @click="toggleLike"
        >
          <span class="feed-card-like-icon">{{ liked ? '♥' : '♡' }}</span>
          <span class="feed-card-like-count">{{ likes }}</span>
        </button>
        <button
          type="button"
          class="feed-card-comment-toggle"
          :class="{ 'is-open': showComments }"
          :title="showComments ? t('feed.card.collapseComments') : t('feed.card.expandComments')"
          :aria-expanded="showComments"
          @click="toggleComments"
        >
          💬 {{ commentCount }}
        </button>
      </div>
      <section v-if="showComments" class="feed-card-comments">
        <form class="feed-card-comment-form" @submit.prevent="submitComment">
          <textarea
            v-model="commentDraft"
            class="feed-card-comment-input"
            rows="2"
            :placeholder="t('feed.card.commentPlaceholder')"
            maxlength="2000"
            :disabled="commentSubmitting"
          />
          <button
            type="submit"
            class="feed-card-comment-submit"
            :disabled="commentSubmitting || !commentDraft.trim()"
          >
            {{ commentSubmitting ? t('feed.card.commentSubmitting') : t('feed.card.commentSubmit') }}
          </button>
        </form>
        <div v-if="commentsLoading && !commentsLoaded" class="feed-card-comments-empty">
          {{ t('feed.card.commentsLoading') }}
        </div>
        <div v-else-if="comments.length === 0" class="feed-card-comments-empty">
          {{ t('feed.card.noComments') }}
        </div>
        <ul v-else class="feed-card-comment-list">
          <li
            v-for="c in comments"
            :key="c.id"
            :class="[
              'feed-card-comment-item',
              {
                'is-character-reply':
                  characterId !== null && c.author_id === characterId,
              },
            ]"
          >
            <div class="feed-card-comment-head">
              <span class="feed-card-comment-author">
                {{ formatAuthor(c) }}
              </span>
              <time class="feed-card-comment-time" :title="c.created_at">
                {{ formatRelative(c.created_at) }}
              </time>
              <button
                v-if="isOwnComment(c)"
                type="button"
                class="feed-card-comment-delete"
                :title="t('common.actions.delete')"
                @click="removeComment(c.id)"
              >
                ×
              </button>
            </div>
            <p class="feed-card-comment-body">{{ c.content_text }}</p>
          </li>
        </ul>
      </section>
    </div>
  </article>
</template>

<style scoped>
.feed-card {
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  overflow: hidden;
}

.feed-card-author {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.02);
  user-select: none;
}

.feed-card-author.is-clickable {
  cursor: pointer;
  transition: background 0.15s;
}

.feed-card-author.is-clickable:hover,
.feed-card-author.is-clickable:focus-visible {
  background: rgba(255, 255, 255, 0.06);
  outline: none;
}

.feed-card-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  overflow: hidden;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  /* IG 風漸層描邊 */
  background: linear-gradient(
    135deg,
    #feda77 0%,
    #f58529 25%,
    #dd2a7b 50%,
    #8134af 75%,
    #515bd4 100%
  );
  padding: 2px;
  box-sizing: border-box;
}

.feed-card-avatar img {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  object-fit: cover;
  background: var(--color-surface);
  border: 2px solid var(--color-bg-secondary);
  box-sizing: border-box;
}

.feed-card-avatar-fallback {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  background: var(--color-bg-secondary);
  color: var(--color-text);
  font-size: 13px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.feed-card-author-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.feed-card-image-link {
  display: block;
  width: 100%;
  background: var(--color-surface);
  aspect-ratio: 1 / 1;
  overflow: hidden;
}

.feed-card-video-wrap {
  display: block;
  width: 100%;
  background: var(--color-surface);
  /* Wan2.2 portrait clips ship at 480×832 (~3:5). Letting the element
     respect the file's native aspect avoids letterboxing while still
     capping height so a long-form clip can't break the card layout. */
  max-height: 70vh;
  overflow: hidden;
}

.feed-card-video {
  width: 100%;
  display: block;
  object-fit: contain;
  background: #000;
}

.feed-card-image-link img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.2s;
}

.feed-card-image-link:hover img {
  transform: scale(1.02);
}

.feed-card-body {
  padding: 8px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.feed-card-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.feed-card-kind {
  color: var(--color-primary-light);
  font-weight: 600;
}

.feed-card-sep {
  opacity: 0.4;
}

.feed-card-source {
  white-space: nowrap;
}

.feed-card-spacer {
  flex: 1;
}

.feed-card-time {
  white-space: nowrap;
  opacity: 0.7;
}

.feed-card-text {
  font-size: 13px;
  line-height: 1.55;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}

.feed-card-reactions {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 11px;
  color: var(--color-text-secondary);
  margin-top: 2px;
}

.feed-card-like,
.feed-card-comment-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: 11px;
  border-radius: 12px;
  cursor: pointer;
  transition: color 0.15s, background 0.15s, transform 0.1s;
}

.feed-card-like:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.05);
  color: #ff6b81;
}

.feed-card-comment-toggle:hover {
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-primary-light);
}

.feed-card-comment-toggle.is-open {
  color: var(--color-primary-light);
}

.feed-card-like.is-liked {
  color: #ff6b81;
}

.feed-card-like.is-pending {
  opacity: 0.7;
  cursor: progress;
}

.feed-card-like:active:not(:disabled),
.feed-card-comment-toggle:active {
  transform: scale(0.95);
}

.feed-card-like-icon {
  font-size: 14px;
  line-height: 1;
}

.feed-card-comments {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 4px;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border);
}

.feed-card-comment-form {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.feed-card-comment-input {
  width: 100%;
  resize: vertical;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  color: var(--color-text);
  font: inherit;
  font-size: 12px;
  padding: 6px 8px;
}

.feed-card-comment-input:focus {
  outline: none;
  border-color: var(--color-primary-light);
}

.feed-card-comment-submit {
  align-self: flex-end;
  padding: 4px 12px;
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text);
  font-size: 11px;
  border-radius: 4px;
  cursor: pointer;
}

.feed-card-comment-submit:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.06);
}

.feed-card-comment-submit:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.feed-card-comments-empty {
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: center;
  padding: 6px;
}

.feed-card-comment-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.feed-card-comment-item {
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* Character-authored reply: tinted left border + subtle gradient so it
   reads as "the character chimed in" without competing with the user's
   own comments. */
.feed-card-comment-item.is-character-reply {
  border-color: rgba(221, 42, 123, 0.35);
  background: linear-gradient(
    90deg,
    rgba(221, 42, 123, 0.08),
    rgba(81, 91, 212, 0.04)
  );
  border-left: 3px solid #dd2a7b;
  padding-left: 8px;
}

.feed-card-comment-item.is-character-reply .feed-card-comment-author {
  color: #f58cbb;
}

.feed-card-comment-head {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.feed-card-comment-author {
  font-weight: 600;
}

.feed-card-comment-time {
  margin-left: auto;
  opacity: 0.7;
}

.feed-card-comment-delete {
  background: transparent;
  border: none;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 4px;
}

.feed-card-comment-delete:hover {
  color: #ff8a75;
}

.feed-card-comment-body {
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0;
}
</style>
