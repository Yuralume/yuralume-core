import { computed, ref } from 'vue'

import type {
  StorySceneEndResponse,
  StorySceneOpenResponse,
  StorySceneSession,
  StorySceneSuggestedAction,
} from '@/types/storyScene'
import { billingRefusalKind } from '@/utils/api/billingRefusal'
import {
  endStoryScene,
  getStoryScene,
  openStoryScene,
} from '@/utils/api/storyScene'
import {
  isStaleSceneState,
  storySceneErrorKey,
} from '@/utils/storySceneErrors'

/**
 * The scene state machine behind the composer's "Start a Scene" control.
 *
 * A factory, not a singleton: the state belongs to one open chat thread, and
 * a module-level ref would leak one character's scene into the next one the
 * player opens (2026-07-26 cross-instance sweep, same lesson one layer up).
 *
 * The composable owns *state*; it never touches the message list. Opening
 * returns the server response so the panel can place the narration and the
 * character's first line in the thread itself — which is where the decision
 * about how a scene looks belongs.
 */
export function useStoryScene() {
  const session = ref<StorySceneSession | null>(null)
  const suggestedActions = ref<StorySceneSuggestedAction[]>([])
  const opening = ref(false)
  const ending = ref(false)
  /** Catalog key of the last refusal, or null. Never a raw server string. */
  const errorKey = ref<string | null>(null)

  /**
   * Guards against a slow answer for a character the player already left.
   * Bumped by every entry point, so a stale response can never install a
   * scene onto the wrong thread.
   */
  let requestSeq = 0

  const isOpen = computed(() => session.value?.status === 'open')

  function clear() {
    requestSeq += 1
    session.value = null
    suggestedActions.value = []
    opening.value = false
    ending.value = false
    errorKey.value = null
  }

  function dismissError() {
    errorKey.value = null
  }

  function setSuggestedActions(actions: StorySceneSuggestedAction[] | null) {
    // Chips only exist inside a scene: outside one they would be a second,
    // competing suggestion surface next to chat assist.
    suggestedActions.value = isOpen.value ? (actions ?? []) : []
  }

  /**
   * Adopt whatever scene the server says is running for this character.
   *
   * Failure is deliberately silent: not being able to *read* the state is not
   * something the player did, and the honest rendering of "we do not know" is
   * the ordinary button — pressing it will produce a real, explained answer.
   */
  async function restore(characterId: string): Promise<void> {
    clear()
    const seq = requestSeq
    try {
      const current = await getStoryScene(characterId)
      if (seq !== requestSeq) return
      session.value = current && current.status === 'open' ? current : null
    } catch {
      if (seq !== requestSeq) return
      session.value = null
    }
  }

  /**
   * Adopt the closed session an in-scene turn brought back with its reply.
   *
   * The turn that answers the scene's dramatic question knows it did, and
   * SC1-D puts that on the reply itself — so the common case needs no
   * second request at all. `sync()` remains the fallback for a backend
   * that predates the field and for turns that never reached a reply.
   *
   * Returns whether it took effect, so the caller can skip the fallback
   * poll. Refuses a session that is not the one on screen (a stale tab, a
   * scene closed and reopened on another device) and refuses to interrupt
   * an open/end already in flight — both own the state more recently than
   * a reply that is being read after the fact.
   */
  function adoptClosed(
    closed: StorySceneSession | null | undefined,
  ): boolean {
    if (!closed || closed.status !== 'closed') return false
    if (opening.value || ending.value) return false
    if (!session.value || session.value.id !== closed.id) return false
    session.value = closed
    suggestedActions.value = []
    return true
  }

  /**
   * Re-read the session after a turn played inside the scene.
   *
   * The story can decide the scene is over — the dramatic question got
   * answered — and nothing in the chat response says so. Without this the
   * player would keep seeing "scene in progress" and an End button over a
   * scene the server already closed. Silent on failure and never cancels an
   * open/end in flight: it is a correction, not an action.
   */
  async function sync(characterId: string): Promise<void> {
    if (!session.value || opening.value || ending.value) return
    const seq = requestSeq
    try {
      const current = await getStoryScene(characterId)
      if (seq !== requestSeq) return
      session.value = current && current.status === 'open' ? current : null
      if (!session.value) suggestedActions.value = []
    } catch {
      // Keep showing what we last knew rather than guessing it ended.
    }
  }

  /**
   * Take the server's word for what is running, under the caller's guard.
   *
   * The recovery behind every `isStaleSceneState` refusal: an action was
   * refused *because* the screen disagrees with the server, so the one thing
   * that helps is to stop arguing and go read. Chips are dropped
   * unconditionally — whatever comes back is by definition not the session
   * they were generated for.
   *
   * Takes the caller's sequence number rather than reading `requestSeq`
   * itself, so a player who left for another character while this was in
   * flight never has a scene installed onto the thread they moved to.
   * Silent on failure, like `restore`: failing to read leaves the state we
   * already had, which the refusal on screen has already explained.
   */
  async function resync(characterId: string, seq: number): Promise<void> {
    try {
      const current = await getStoryScene(characterId)
      if (seq !== requestSeq) return
      session.value = current && current.status === 'open' ? current : null
      suggestedActions.value = []
    } catch {
      // Nothing to say: the refusal that sent us here is already on screen.
    }
  }

  /**
   * Raise the curtain. Returns the opening payload on success, else null —
   * the refusal is already on screen via `errorKey`.
   *
   * The two money refusals are the exception and are **rethrown** (SC3-C).
   * "You are out of Lumes" is answered by a shared card with a top-up link
   * and "the price moved" has to re-pull the price list; neither is a
   * sentence this state machine can say, and flattening them into a scene
   * error would replace a card the player can act on with a line they
   * cannot. The scene state is left exactly as it was — nothing opened,
   * nothing was charged.
   */
  async function open(
    characterId: string,
  ): Promise<StorySceneOpenResponse | null> {
    if (opening.value || ending.value || isOpen.value) return null
    const seq = ++requestSeq
    opening.value = true
    errorKey.value = null
    try {
      const response = await openStoryScene(characterId)
      if (seq !== requestSeq) return null
      session.value = response.session
      suggestedActions.value = response.suggested_actions ?? []
      return response
    } catch (error) {
      if (seq !== requestSeq) return null
      if (billingRefusalKind(error) !== null) throw error
      errorKey.value = storySceneErrorKey(error, 'generic')
      // `scene_in_progress` is only reachable when this tab does not know
      // about the scene — the guard above would have refused the press
      // otherwise. So the scene is real and invisible here, and leaving it
      // that way strands the player with an apology for something they can
      // neither see nor close. Adopting it costs one read and turns the
      // refusal into a scene with a working End button.
      if (isStaleSceneState(error)) await resync(characterId, seq)
      return null
    } finally {
      if (seq === requestSeq) opening.value = false
    }
  }

  /**
   * Bring the curtain down early. Returns the closing payload on success.
   *
   * While SC1-D is still landing the backend answers `501`, which reaches the
   * player as a plain "not yet" — the scene stays open and nothing is lost.
   *
   * The two `409`s about *which* scene is running are the opposite case and
   * must not leave the state alone: they say the scene on screen is already
   * over (a verdict, the timeout sweep, another tab), so keeping it would
   * leave the player staring at "scene in progress" above a button whose
   * only possible outcome is the same refusal again. Those resync instead.
   */
  async function end(
    characterId: string,
  ): Promise<StorySceneEndResponse | null> {
    const current = session.value
    if (!current || current.status !== 'open' || ending.value) return null
    const seq = ++requestSeq
    ending.value = true
    errorKey.value = null
    try {
      const response = await endStoryScene(characterId, current.id)
      if (seq !== requestSeq) return null
      session.value = response.session
      suggestedActions.value = []
      return response
    } catch (error) {
      if (seq !== requestSeq) return null
      // The refusal is still shown — the player pressed a button and is owed
      // an answer — but it no longer blocks the correction underneath it.
      errorKey.value = storySceneErrorKey(error, 'endGeneric')
      if (isStaleSceneState(error)) await resync(characterId, seq)
      return null
    } finally {
      if (seq === requestSeq) ending.value = false
    }
  }

  return {
    session,
    suggestedActions,
    opening,
    ending,
    errorKey,
    isOpen,
    clear,
    dismissError,
    setSuggestedActions,
    adoptClosed,
    restore,
    sync,
    open,
    end,
  }
}

export type StorySceneController = ReturnType<typeof useStoryScene>
