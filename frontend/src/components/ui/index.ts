export { default as UiButton } from './UiButton.vue'
export { default as UiInput } from './UiInput.vue'
export { default as UiTextarea } from './UiTextarea.vue'
export { default as UiSelect } from './UiSelect.vue'
export type { UiSelectOption } from './UiSelect.vue'
export { default as UiCombobox } from './UiCombobox.vue'
export { default as UiImage } from './UiImage.vue'
export type { UiImageVariant, UiImageSizeKey } from './UiImage.vue'
export {
  UI_IMAGE_VARIANT_QUERY,
  imageSrcset,
  imageVariantUrl,
  supportsSizeVariants,
  uiImageVariantPlan,
} from './UiImage.vue'
export { default as UiLightbox } from './UiLightbox.vue'
export type {
  LightboxItem,
  LightboxLoadMoreRequestInput,
  LightboxLoadMoreState,
  LightboxLoadMoreStateInput,
  LightboxPendingLoad,
  LightboxResumeAction,
  LightboxResumeActionInput,
  LightboxResumeInput,
  LightboxResumeOwedInput,
  LightboxStep,
  LightboxStepInput,
  LightboxStepOutcome,
  LightboxSwipeAction,
  LightboxSwipeInput,
} from '@/utils/lightbox'
// 續載那一族（`lightboxResumeAction` / `isLightboxResumeOwed` / 那兩個型別）
// 目前的唯一消費者是 `UiLightbox` 自己——擺在這裡不是因為現在有人從外面用，
// 而是因為半族在 barrel 裡半族不在，下一個接續載的消費者從 `@/components/ui`
// 匯入時會看不到判準、於是自己再寫一份（這正是上一輪邊界補償的來歷）。
export {
  LIGHTBOX_SWIPE_THRESHOLD_PX,
  classifyLightboxSwipe,
  isLightboxResumeOwed,
  lightboxItemAt,
  lightboxLoadMoreState,
  lightboxNeighbourUrls,
  lightboxResumeAction,
  resumeLightboxAfterLoad,
  shouldSendLightboxLoadMore,
  shouldShowLightboxCounter,
  shouldShowLightboxNav,
  stepLightboxIndex,
  wrapLightboxIndex,
} from '@/utils/lightbox'
export { default as UiCard } from './UiCard.vue'
export { default as UiSection } from './UiSection.vue'
export { default as UiBadge } from './UiBadge.vue'
export { default as UiProgressRing } from './UiProgressRing.vue'
