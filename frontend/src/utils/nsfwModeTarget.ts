/**
 * NSFW mode target — image column selection mapping.
 *
 * The API stores "image generation explicitly off" as
 * `image_profile_id: null` — never a sentinel string, so no fake
 * profile id can flow into registry lookups. A native `<select>` cannot
 * hold null, so the picker uses this UI-local sentinel and maps it back
 * to null at the API seam. The sentinel must never appear in a payload.
 */
export const NSFW_IMAGE_GENERATION_OFF = '__nsfw_image_generation_off__'

export function imageProfileSelectionFromTarget(
  imageProfileId: string | null,
): string {
  return imageProfileId ?? NSFW_IMAGE_GENERATION_OFF
}

export function imageProfileIdForSave(selection: string): string | null {
  return selection === NSFW_IMAGE_GENERATION_OFF ? null : selection
}

/**
 * LLM provider and model stay mandatory; the image column is a
 * two-way choice — a profile or the explicit off option. An empty
 * selection (nothing picked yet) keeps save blocked: off is an
 * explicit choice, not a blank default.
 */
export function hasSelectableNsfwTarget(input: {
  llmProviderId: string
  llmModelId: string
  imageProfileSelection: string
}): boolean {
  return (
    input.llmProviderId.length > 0
    && input.llmModelId.length > 0
    && input.imageProfileSelection.length > 0
  )
}
