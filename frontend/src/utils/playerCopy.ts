/**
 * The cloud-vs-self-host copy rule (plan U1), as a pure function.
 *
 * The hosted product is aimed at non-technical players, so a growing set of
 * strings needs a second, plainer wording — while the self-host catalogue
 * stays byte-for-byte unchanged (an operator *wants* to read "ComfyUI",
 * "YAML", "VAPID": those are actionable leads for them).
 *
 * Rather than sprinkling `cloudMode ? t(a) : t(b)` across two dozen
 * components, the convention is a sibling key suffixed `Cloud`:
 *
 *   albumPanel.hint       ← self-host wording (unchanged, always present)
 *   albumPanel.hintCloud  ← player wording (optional, hosted only)
 *
 * Kept free of Vue and of `useAuth` so the rule itself is unit-testable.
 */

/** Suffix marking a key as the hosted-player variant of its sibling. */
export const CLOUD_COPY_SUFFIX = 'Cloud'

/**
 * Which catalogue key a surface should render.
 *
 * Degrades to `key` in two directions, both load-bearing: self-host never
 * consults a variant at all, and a hosted surface whose variant has not been
 * written yet shows the operator wording instead of a missing-key blank.
 */
export function resolveCopyKey(
  key: string,
  cloudMode: boolean,
  hasCloudVariant: (candidate: string) => boolean,
): string {
  if (!cloudMode) return key
  if (key.endsWith(CLOUD_COPY_SUFFIX)) return key
  const candidate = `${key}${CLOUD_COPY_SUFFIX}`
  return hasCloudVariant(candidate) ? candidate : key
}
