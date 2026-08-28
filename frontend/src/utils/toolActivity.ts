/** Tool-activity indicator state for the chat typing indicator.
 *
 * The streaming chat endpoint interleaves ``{"tool_activity": {tool,
 * status}}`` frames while the character's tool cycle runs (image
 * generation, web search, …). The typing indicator upgrades from bare
 * dots to a small icon + diegetic line while a tool is actually
 * running — replacing the old always-on "maybe using a tool" guess.
 *
 * Pure functions here (no Vue) so the SSR test harness can pin the
 * mapping and the state transitions; ``ChatPanel`` only wires events
 * through them.
 */

export interface ToolActivityEvent {
  tool: string
  status: string
}

export interface ToolActivityDisplay {
  icon: string
  labelKey: string
}

/** Diegetic per-tool presentation. Unknown tools (future additions,
 * self-host custom tools) fall back to a generic "busy" line rather
 * than leaking the raw tool name to the player. */
const KNOWN_TOOLS: Record<string, ToolActivityDisplay> = {
  generate_image: { icon: '🎨', labelKey: 'chat.toolActivity.generateImage' },
  web_search: { icon: '🔍', labelKey: 'chat.toolActivity.webSearch' },
  web_fetch: { icon: '📄', labelKey: 'chat.toolActivity.webFetch' },
}

const GENERIC: ToolActivityDisplay = {
  icon: '✨',
  labelKey: 'chat.toolActivity.generic',
}

export function toolActivityDisplay(tool: string): ToolActivityDisplay {
  return KNOWN_TOOLS[tool] ?? GENERIC
}

/** Reducer for the panel's "which tool is running" state.
 *
 * - ``started`` always wins (multi-hop chains switch the icon to the
 *   newest tool).
 * - ``finished`` only clears the state when it matches the tool that
 *   set it — a stale finish from an earlier hop must not blank the
 *   icon of a tool that just started.
 * - Unknown statuses keep the current state (forward compatibility:
 *   an older bundle facing a newer backend must not glitch).
 */
export function nextActiveTool(
  current: string | null,
  event: ToolActivityEvent,
): string | null {
  if (typeof event.tool !== 'string' || event.tool.length === 0) return current
  if (event.status === 'started') return event.tool
  if (event.status === 'finished') {
    return current === event.tool ? null : current
  }
  return current
}
