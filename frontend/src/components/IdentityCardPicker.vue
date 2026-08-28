<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiSelect } from '@/components/ui'
import { listIdentityCards, type IdentityCard } from '@/utils/api/identityCards'

/**
 * 創角精靈頂部的「帶入身分卡」picker（IC2）。
 *
 * 抽成獨立元件而不是塞進精靈裡，是為了讓「什麼時候去抓清單、抓不到怎麼辦」
 * 只有一個答案：精靈只收到一個 `select` 事件，不必知道清單怎麼來的。
 *
 * 兩條刻意的行為：
 * - **沒有卡就整段不出現**。空的下拉是一個需要玩家自己看懂「因為你還沒存過
 *   卡」的謎題。
 * - **每次開精靈重抓**。玩家很可能上一次建角時才剛存了一張卡，快取住清單會
 *   讓那張卡在下一次創角時神隱。
 *
 * 讀不到清單時 fail-soft 當作沒有卡：精靈本來就能從空白填，為了一個附加的
 * 捷徑擋住創角是本末倒置。
 */
const props = defineProps<{
  /** 精靈是否開著。轉為 true 時重抓清單並清掉上次的選擇。 */
  active: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [card: IdentityCard]
}>()

const { t } = useI18n()

const cards = ref<IdentityCard[]>([])
const selectedId = ref('')
let loadToken = 0

watch(() => props.active, (active) => {
  if (!active) return
  selectedId.value = ''
  void reload()
}, { immediate: true })

async function reload() {
  const token = ++loadToken
  try {
    const { cards: fetched } = await listIdentityCards()
    if (token !== loadToken) return
    cards.value = fetched
  } catch {
    if (token !== loadToken) return
    cards.value = []
  }
}

const options = computed(() => cards.value.map(card => ({
  value: card.id,
  label: card.name,
})))

/**
 * 選一張卡 → 發事件 → 立刻把下拉退回 placeholder。
 *
 * 這個下拉是**動作觸發器**，不是一個「目前選了哪張卡」的狀態欄位——套卡是
 * 複製快照，套完之後表單與卡片再無關聯，把卡名留在框裡只會謊稱兩者還連著。
 * 更實際的理由是原生 `<select>` 只在值**改變**時發 `change`：值若留著，玩
 * 家把表單改壞想重套同一張卡時，點下去完全沒有反應（沒有錯誤、沒有提示，
 * 就是不動），而「套了、亂改、想重來」是這個 picker 最常見的用法。
 */
function onSelect(value: string) {
  const card = cards.value.find(item => item.id === value)
  if (card) emit('select', card)
  selectedId.value = ''
}
</script>

<template>
  <div v-if="cards.length" class="identity-card-picker">
    <UiSelect
      :model-value="selectedId"
      :options="options"
      :disabled="disabled"
      :label="t('identityCard.picker.label')"
      :placeholder="t('identityCard.picker.placeholder')"
      @update:model-value="onSelect"
    />
    <p class="field-hint">{{ t('identityCard.picker.hint') }}</p>
  </div>
</template>

<style scoped>
.identity-card-picker {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
</style>
