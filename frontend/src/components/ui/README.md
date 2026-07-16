# `components/ui/` — 共用 UI 元件層

這層放**無業務邏輯**的視覺基礎元件，給上層 panel / page 組合用。任何「跟使用者長期記憶 / 後端 API / 角色狀態」相關的邏輯一律**不准**寫在這裡。

## 樣式四層原則

| 層 | 位置 | 性質 |
|---|---|---|
| 1. **Tokens** | `frontend/src/style.css` `:root` 區段 | CSS 變數：色票、間距、radius、字體尺寸 |
| 2. **Base classes** | `frontend/src/style.css` 全域區 | `.field-input` / `.ui-btn` / `.ui-card` / `.ui-badge` — 給 UI primitives 內部與過渡期手寫使用 |
| 3. **UI primitives** | 本目錄 `*.vue` | 把 base classes 包成 Vue 元件 + props + slot；零業務 |
| 4. **Panels / Pages** | `components/*.vue` / `pages/*.vue` | 組合 UI primitives + 接 API；scoped style 只放版面微調，禁止重貼 base 視覺 |

**禁止事項**：
- 禁止在 panel scoped style 重貼 `.btn` / `.btn-primary` / `.field-input` 等基礎視覺
- 禁止在 UI primitives 內部 `import` API utility 或 store
- 禁止在 UI primitives 添加業務語意（例如 `<UiButton character-id="...">` 這種 prop）

## 元件清單

| 元件 | 用途 | 主要 props |
|---|---|---|
| `UiButton` | 統一按鈕 | `variant: primary \| secondary \| danger \| ghost \| chip \| hero`, `size: sm \| md \| lg`, `loading`, `block`, `active` |
| `UiInput` | 文字輸入（含 number / date / password 等 type） | `modelValue`, `label`, `hint`, `type`, `placeholder`, `disabled`, `readonly`, `required` |
| `UiTextarea` | 多行文字 | `modelValue`, `label`, `hint`, `rows`, `maxlength` |
| `UiSelect` | 下拉選單（深色 option 已處理） | `modelValue`, `options[]`, `placeholder`；也可用 default slot 自行寫 `<option>` |
| `UiCard` | 卡片容器 | `size`, `hoverable`, `title`；slots: `header` / `actions` / `default` / `footer` |
| `UiSection` | 表單分組 | `title`, `description`, `bordered`；slots: `header` / `default` |
| `UiBadge` | 狀態徽章 | `variant: default \| primary \| success \| warning \| danger` |

## 使用範例

```vue
<script setup lang="ts">
import { ref } from 'vue'
import { UiButton, UiInput, UiCard, UiSection } from '@/components/ui'

const name = ref('')
</script>

<template>
  <UiCard title="基本資料">
    <UiSection title="名稱" description="角色顯示名稱，創建後可改但會造成記憶漂移">
      <UiInput v-model="name" label="名稱" placeholder="角色名稱" required />
    </UiSection>
    <template #footer>
      <UiButton variant="primary" :loading="saving" @click="save">儲存</UiButton>
    </template>
  </UiCard>
</template>
```

## 何時新增元件？

當你發現**至少 3 個地方需要相同的視覺 + 互動**，就該抽 UI primitive。否則直接在 panel 內手刻即可，避免過早抽象。

## 即時驗收

開啟 dev route `/_styleguide`（`pages/StyleGuidePage.vue`）查看所有 ui 元件的 variant / size / state，當作回歸基準。
