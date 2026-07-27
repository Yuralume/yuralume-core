/**
 * Conversion affordances shown when the public demo cannot be entered.
 *
 * This module owns *where* each action goes (build-time overridable URLs);
 * it deliberately owns no copy. Labels are i18n keys resolved at the display
 * boundary, so the demo pages localise like the rest of the SPA instead of
 * staying the last English-only surface (plan U1-D).
 */
const DEFAULT_TIER0_URL = '/#demo-showcase'
const DEFAULT_WAITLIST_URL = '/#alpha'
const DEFAULT_DISCORD_URL = 'https://discord.gg/tF8zw7S6'
const DEFAULT_SELF_HOST_URL = '/#tiers'

export interface DemoConversionAction {
  /** i18n key under `demo.actions.*` — never a display string. */
  labelKey: string
  href: string
  external: boolean
  variant: 'primary' | 'secondary'
}

export interface DemoConversionLinks {
  tier0Url: string
  waitlistUrl: string
  discordUrl: string
  selfHostUrl: string
}

export function demoConversionLinks(): DemoConversionLinks {
  return {
    tier0Url: envUrl('VITE_YURALUME_DEMO_TIER0_URL', DEFAULT_TIER0_URL),
    waitlistUrl: envUrl('VITE_YURALUME_DEMO_WAITLIST_URL', DEFAULT_WAITLIST_URL),
    discordUrl: envUrl('VITE_YURALUME_DEMO_DISCORD_URL', DEFAULT_DISCORD_URL),
    selfHostUrl: envUrl('VITE_YURALUME_DEMO_SELF_HOST_URL', DEFAULT_SELF_HOST_URL),
  }
}

export function demoRetryActions(): DemoConversionAction[] {
  const links = demoConversionLinks()
  return [
    action('demo.actions.tier0', links.tier0Url, 'primary'),
    action('demo.actions.waitlist', links.waitlistUrl, 'secondary'),
    action('demo.actions.discord', links.discordUrl, 'secondary'),
  ]
}

export function demoUnavailableActions(): DemoConversionAction[] {
  const links = demoConversionLinks()
  return [
    action('demo.actions.tier0', links.tier0Url, 'primary'),
    action('demo.actions.discord', links.discordUrl, 'secondary'),
    action('demo.actions.selfHost', links.selfHostUrl, 'secondary'),
  ]
}

export function demoMaxMessagesActions(): DemoConversionAction[] {
  const links = demoConversionLinks()
  return [
    action('demo.actions.waitlist', links.waitlistUrl, 'primary'),
    action('demo.actions.selfHost', links.selfHostUrl, 'secondary'),
    action('demo.actions.discord', links.discordUrl, 'secondary'),
  ]
}

function action(
  labelKey: string,
  href: string,
  variant: DemoConversionAction['variant'],
): DemoConversionAction {
  return {
    labelKey,
    href,
    external: isExternalUrl(href),
    variant,
  }
}

function envUrl(key: keyof ImportMetaEnv, fallback: string): string {
  const value = import.meta.env[key]
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

function isExternalUrl(href: string): boolean {
  return /^https?:\/\//i.test(href)
}
