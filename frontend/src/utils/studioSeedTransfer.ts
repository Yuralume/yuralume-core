/**
 * In-memory handoff for creator-form seeds (Creator Studio C1).
 *
 * Why not the URL: seed prompts carry private chat / memoir prose. A
 * `?seedPrompt=` query rides the address bar, window.history, and — on
 * any same-origin request fired before the page strips it — the Referer
 * header (browser default `strict-origin-when-cross-origin` sends the
 * full path+query to same-origin endpoints), which lands verbatim in
 * access logs / proxies / APM. Private creation material must never
 * take that path, so in-app entrances stash the seed here and navigate
 * with a bare route instead.
 *
 * Take-once semantics: `takeStudioSeed` clears the stash, so a refresh
 * or revisit of the creator page never replays a stale seed. The stash
 * dies with the tab — exactly the lifetime a one-shot form prefill
 * needs. (The creator pages keep a query fallback for future canned,
 * non-sensitive deep links; those pages strip the query before firing
 * any API call.)
 */

export interface StudioSeedHandoff {
  seedPrompt: string
  /** Character ids to preselect; receiving page filters to owned ids. */
  cast?: string[]
}

let pending: StudioSeedHandoff | null = null

export function stashStudioSeed(handoff: StudioSeedHandoff): void {
  pending = handoff
}

export function takeStudioSeed(): StudioSeedHandoff | null {
  const handoff = pending
  pending = null
  return handoff
}
