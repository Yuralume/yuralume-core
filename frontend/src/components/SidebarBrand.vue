<!--
  SidebarBrand
  ------------------------------------------------------------
  共用品牌區塊：logo mark + "Yuralume" wordmark + 可選副標。
  目前由 PlayerSidebar 與 AdminLayout 使用，保證左上 brand 區
  在主畫面與 admin 之間視覺完全一致 (尺寸、字級、padding)。

  - 無 `to` props：純展示 (PlayerSidebar 用)
  - 有 `to` props：包成 RouterLink，整塊可點 (AdminLayout 返回玩家頁用)
-->
<script setup lang="ts">
import { RouterLink, type RouteLocationRaw } from 'vue-router'

defineProps<{
  /** 副標文字。空字串或 undefined 時不渲染。 */
  subtitle?: string
  /** 有值時整塊變成可點 RouterLink。 */
  to?: RouteLocationRaw
  /** RouterLink 的 title 屬性 (hover 提示)。僅在 `to` 有值時生效。 */
  linkTitle?: string
}>()
</script>

<template>
  <RouterLink
    v-if="to"
    :to="to"
    :title="linkTitle"
    class="brand-block brand-block--link"
  >
    <div class="brand-row">
      <img src="/logo-mark.png" alt="" class="brand-mark" aria-hidden="true" />
      <div class="brand-text">
        <h1 class="brand">Yuralume</h1>
        <div v-if="subtitle" class="subtitle">{{ subtitle }}</div>
      </div>
    </div>
  </RouterLink>
  <div v-else class="brand-block">
    <div class="brand-row">
      <img src="/logo-mark.png" alt="" class="brand-mark" aria-hidden="true" />
      <div class="brand-text">
        <h1 class="brand">Yuralume</h1>
        <div v-if="subtitle" class="subtitle">{{ subtitle }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.brand-block {
  display: block;
  padding: 20px 16px 12px;
  border-bottom: 1px solid var(--color-border);
}
.brand-block--link {
  text-decoration: none;
  color: inherit;
  transition: background-color 0.15s;
}
.brand-block--link:hover {
  background: rgba(255, 255, 255, 0.04);
}
.brand-row {
  display: flex;
  align-items: center;
  gap: 10px;
}
.brand-mark {
  width: 28px;
  height: 28px;
  object-fit: contain;
  flex-shrink: 0;
}
.brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
  min-width: 0; /* 讓 subtitle 能正確 truncate, 不撐爆 sidebar */
}
.brand {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 500;
  letter-spacing: 0.04em;
  color: var(--color-primary);
  margin: 0;
  line-height: 1;
}
.subtitle {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-top: 2px;
}
</style>
