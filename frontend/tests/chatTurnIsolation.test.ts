/**
 * One panel, many characters: a turn must never write into a thread the
 * reader has already left.
 *
 * `<ChatPanel>` is mounted once and handed a different character as the
 * reader taps down the sidebar, so a turn that is still streaming outlives
 * the screen it was composed for. Three things were bleeding through that
 * seam: the streaming animation kept typing into the next character's
 * thread, the reply and its state were grafted onto that character, and a
 * failure was reported as theirs.
 *
 * Layered the way the fix is:
 *   1. `isTurnStillCurrent` — the one branch that decides whether a result
 *      may be shown — pinned as a pure function.
 *   2. `ChatTurnGuard` — the bookkeeping around it (abort + epoch), driven
 *      through the real switch sequences.
 *   3. The guard against the real transport: a character switch mid-stream,
 *      end to end, with `sendChatMessageStream` doing the streaming.
 *   4. `ChatPanel`'s wiring, read from source — the file is too large and
 *      dependency-heavy to render here (same reasoning as
 *      `stageNudgeSurface.test.ts`), and what regressed is precisely
 *      *where* the resets and the re-checks sit.
 */

import { readFileSync } from 'node:fs'

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ChatTurnGuard,
  isTurnStillCurrent,
  shouldDiscardTurnResult,
} from '@/utils/chatTurnGuard'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

const { authedFetch } = await import('@/utils/authedFetch')
const { ChatStreamAbortedError, sendChatMessageStream }
  = await import('@/utils/api/chat')

const mockedAuthedFetch = vi.mocked(authedFetch)

beforeEach(() => {
  vi.clearAllMocks()
})

// ----------------------------------------------------------------------
// 1. The pure decision
// ----------------------------------------------------------------------

describe('isTurnStillCurrent', () => {
  it('lets a turn write when nothing moved under it', () => {
    expect(isTurnStillCurrent(
      { epoch: 3, characterId: 'char-a' },
      { epoch: 3, characterId: 'char-a' },
    )).toBe(true)
  })

  it('disowns a turn once the panel walked away from it', () => {
    // The epoch is what makes "switch away and come straight back" safe:
    // the character id matches again, but the turn started a generation ago.
    expect(isTurnStillCurrent(
      { epoch: 3, characterId: 'char-a' },
      { epoch: 5, characterId: 'char-a' },
    )).toBe(false)
  })

  it('disowns a turn composed for a different character', () => {
    expect(isTurnStillCurrent(
      { epoch: 3, characterId: 'char-a' },
      { epoch: 3, characterId: 'char-b' },
    )).toBe(false)
  })

  it('treats "no character on screen" as never current', () => {
    // Unmount, or the sidebar's empty state: there is nothing to write to.
    expect(isTurnStillCurrent(
      { epoch: 3, characterId: 'char-a' },
      { epoch: 3, characterId: null },
    )).toBe(false)
    expect(isTurnStillCurrent(
      { epoch: 3, characterId: null },
      { epoch: 3, characterId: null },
    )).toBe(false)
  })

  it('reads the same either way round', () => {
    const ticket = { epoch: 1, characterId: 'char-a' }
    expect(shouldDiscardTurnResult(ticket, { epoch: 2, characterId: 'char-a' }))
      .toBe(true)
    expect(shouldDiscardTurnResult(ticket, { epoch: 1, characterId: 'char-a' }))
      .toBe(false)
  })
})

// ----------------------------------------------------------------------
// 2. The guard around it
// ----------------------------------------------------------------------

describe('ChatTurnGuard', () => {
  it('hands a begun turn a live signal it still owns', () => {
    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')

    expect(ticket.signal.aborted).toBe(false)
    expect(guard.isCurrent(ticket)).toBe(true)
    expect(guard.inFlight).toBe(true)
  })

  it('aborts and disowns the turn when the character changes', () => {
    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')

    guard.interrupt('char-b')

    expect(ticket.signal.aborted).toBe(true)
    // Belt *and* braces: even a result that resolves anyway is refused.
    expect(guard.isCurrent(ticket)).toBe(false)
    expect(guard.inFlight).toBe(false)
  })

  it('keeps disowning it after the reader switches back', () => {
    // The case a character-id check alone gets wrong: tap away, tap back,
    // and the abandoned turn's closure would find its own id on screen.
    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')

    guard.interrupt('char-b')
    guard.interrupt('char-a')

    expect(guard.isCurrent(ticket)).toBe(false)
  })

  it('disowns everything on unmount', () => {
    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')

    guard.interrupt(null)

    expect(ticket.signal.aborted).toBe(true)
    expect(guard.isCurrent(ticket)).toBe(false)
  })

  it('lets the character carry on sending after a turn settles', () => {
    const guard = new ChatTurnGuard()
    const first = guard.begin('char-a')
    guard.settle(first)
    const second = guard.begin('char-a')

    expect(guard.isCurrent(second)).toBe(true)
    expect(second.signal.aborted).toBe(false)
    expect(guard.isCurrent(first)).toBe(true) // same epoch, same character
  })

  it('never lets a finished turn disarm the one that replaced it', () => {
    // `settle` runs in a `finally`, which can execute *after* the next turn
    // has begun. Clearing the controller blindly there would leave the new
    // stream with nothing to cancel it on the next switch.
    const guard = new ChatTurnGuard()
    const first = guard.begin('char-a')
    const second = guard.begin('char-a')

    guard.settle(first)
    expect(guard.inFlight).toBe(true)

    guard.interrupt('char-b')
    expect(second.signal.aborted).toBe(true)
  })

  it('cancels a still-running turn when a new one begins', () => {
    const guard = new ChatTurnGuard()
    const first = guard.begin('char-a')
    guard.begin('char-a')

    expect(first.signal.aborted).toBe(true)
  })

  it('hands the screen to the newest turn without disowning the older one', () => {
    // Two turns on the same character at once is a designed state, not a
    // race: the multi-bubble reveal returns the composer while the first
    // turn is still finishing. Which means the two questions come apart —
    // the first turn's *result* is still wanted (its reply, its closed
    // scene, its send-off narration all belong in this thread), while the
    // *screen* — one tool indicator, one row of scene chips — is the
    // second's. Bumping the epoch in `begin` would have answered both with
    // "no" and quietly eaten a paid-for closing narration.
    const guard = new ChatTurnGuard()
    const first = guard.begin('char-a')
    const second = guard.begin('char-a')

    expect(guard.isCurrent(first)).toBe(true)
    expect(guard.ownsScreen(first)).toBe(false)
    expect(guard.ownsScreen(second)).toBe(true)
  })

  it('takes the screen off a turn the reader walked away from', () => {
    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')

    guard.interrupt('char-b')

    // Newest turn *and* still current: failing either is enough.
    expect(guard.ownsScreen(ticket)).toBe(false)
  })
})

// ----------------------------------------------------------------------
// 3. Guard + real transport: the switch that happens mid-stream
// ----------------------------------------------------------------------

describe('a character switch mid-stream', () => {
  it('cuts the stream off and refuses the reply that arrives anyway', async () => {
    const stream = controllableStreamResponse()
    mockedAuthedFetch.mockResolvedValueOnce(stream.response)

    const guard = new ChatTurnGuard()
    const ticket = guard.begin('char-a')
    const streamed: string[] = []
    const conversationIds: string[] = []
    const settled = sendChatMessageStream(
      { character_id: 'char-a', message: 'hello' },
      // The panel wires exactly this guard into its callbacks, which is why
      // the abandoned turn cannot type into the next character's thread.
      (token) => { if (guard.isCurrent(ticket)) streamed.push(token) },
      (id) => { if (guard.isCurrent(ticket)) conversationIds.push(id) },
      undefined,
      { signal: ticket.signal },
    ).catch((error: unknown) => error)

    stream.push('data: {"conversation_id":"conv-a"}\n\n')
    stream.push('data: {"token":"我在"}\n\n')
    await vi.waitFor(() => expect(streamed).toEqual(['我在']))

    // The reader taps the next character in the sidebar.
    guard.interrupt('char-b')

    // What the server had already sent keeps coming, and the turn even
    // finishes — none of it is the new character's.
    stream.push('data: {"token":"這裡"}\n\n')
    stream.push('data: {"done":true,"response":{"conversation_id":"conv-a"}}\n\n')

    const outcome = await settled
    expect(outcome).toBeInstanceOf(ChatStreamAbortedError)
    expect(streamed).toEqual(['我在'])
    expect(conversationIds).toEqual(['conv-a'])
    // ...and the closure that resumes after the await is refused outright,
    // whatever it is holding.
    expect(guard.isCurrent(ticket)).toBe(false)
  })

  it('leaves the new character free to send immediately', async () => {
    // The lock-release half: abandoning must not leave the composer stuck
    // behind a turn that will never report back.
    const stream = controllableStreamResponse()
    mockedAuthedFetch.mockResolvedValueOnce(stream.response)

    const guard = new ChatTurnGuard()
    const abandoned = guard.begin('char-a')
    const settled = sendChatMessageStream(
      { character_id: 'char-a', message: 'hello' },
      () => {},
      undefined,
      undefined,
      { signal: abandoned.signal },
    ).catch((error: unknown) => error)

    guard.interrupt('char-b')
    expect(await settled).toBeInstanceOf(ChatStreamAbortedError)

    const fresh = guard.begin('char-b')
    expect(guard.isCurrent(fresh)).toBe(true)
    expect(fresh.signal.aborted).toBe(false)
  })
})

// ----------------------------------------------------------------------
// 4. ChatPanel's wiring
// ----------------------------------------------------------------------

// Normalised: the working tree carries CRLF here, and every marker below is
// written with plain newlines.
const panelSource = readFileSync(
  new URL('../src/components/ChatPanel.vue', import.meta.url),
  'utf8',
).replace(/\r\n/g, '\n')

/** The slice between two markers — searched inside `haystack` if given. */
function section(from: string, to: string, haystack = panelSource): string {
  const start = haystack.indexOf(from)
  expect(start, `missing marker: ${from}`).toBeGreaterThan(-1)
  const end = haystack.indexOf(to, start + from.length)
  expect(end, `missing marker: ${to}`).toBeGreaterThan(start)
  return haystack.slice(start, end)
}

describe('ChatPanel abandons the turn it walks away from', () => {
  // Bounded to the function body: the next declaration, not the next thing
  // that happens to mention a reset. Half these lines appear elsewhere in the
  // file, and a section spanning them would pass without the reset existing.
  const reset = section('function abandonInFlightTurn(', '\ntype ChatInteractionMode')

  it('resets everything the stream owns on screen in one place', () => {
    expect(reset).toContain('turnGuard.interrupt(nextCharacterId)')
    expect(reset).toContain("streamingText.value = ''")
    expect(reset).toContain('activeToolName.value = null')
    // Not `releaseSendingLock(id)`: that one is id-checked and would leave
    // the composer disabled for ever when the turn is disowned, not ended.
    expect(reset).toContain('abandonSendingLock()')
    expect(reset).toContain('revealingMessageIndex.value = null')
  })

  it('drops the refusal cards, which answered about the thread it left', () => {
    // Every one of these is a statement about *this* conversation — its
    // message cap, its failed upload, the price of the turn it just refused.
    // Carried across a switch they are a false accusation pinned above a
    // thread that was never refused anything, and nothing else clears them:
    // the next send does, but the player has to send first to find out.
    expect(reset).toContain('sessionMessageCapReached.value = false')
    expect(reset).toContain('creditsExhausted.value = false')
    expect(reset).toContain('creditsRequiredCr.value = null')
    expect(reset).toContain('uploadError.value = null')
  })

  it('disowns the undo in flight too, not just the stream', () => {
    // The undo is a second long request holding three controls hostage
    // (undo, open-a-scene, load-older). Left armed, the new character's
    // buttons stay disabled until somebody else's request comes back.
    expect(reset).toContain('abandonUndoRequest()')
  })

  it('runs that reset first thing when the character changes', () => {
    const watcher = section(
      'watch(() => props.character?.id ?? null,', 'focusInput()',
    )
    expect(watcher).toContain('abandonInFlightTurn(characterId)')
  })

  it('runs it on unmount too', () => {
    const unmount = section(
      'onUnmounted(() => {\n  if (activityTimer) clearInterval(activityTimer)',
      '})',
    )
    expect(unmount).toContain('abandonInFlightTurn(null)')
  })
})

describe('ChatPanel guards the turn across every await', () => {
  const runChatTurn = section(
    'async function runChatTurn(', '\nasync function handleSend()',
  )

  it('hands the stream the signal that cancels it', () => {
    expect(runChatTurn).toContain('{ signal: ticket.signal }')
  })

  it('re-checks the stamp after the stream, before touching the thread', () => {
    const guardAt = runChatTurn.indexOf('if (!turnGuard.isCurrent(ticket)) return')
    const pushAt = runChatTurn.indexOf(
      'localMessages.value.push(reply.assistant_message)',
    )
    expect(guardAt).toBeGreaterThan(-1)
    expect(pushAt).toBeGreaterThan(guardAt)
  })

  it('re-checks it inside the streaming callbacks as well', () => {
    // The tick the abort loses: a frame already in hand when the switch
    // happened would otherwise still be appended to `streamingText`.
    const callbacks = section(
      'const reply = await sendChatMessageStream(',
      '{ signal: ticket.signal },',
    )
    expect(callbacks.match(/turnGuard\.isCurrent\(ticket\)/g)?.length)
      .toBeGreaterThanOrEqual(3)
  })

  it('treats an abort as an outcome, never as an error bubble', () => {
    const failurePath = section('} catch (err) {', '} finally {', runChatTurn)
    const guardAt = failurePath.indexOf(
      'if (isChatStreamAbortedError(err) || !turnGuard.isCurrent(ticket)) return',
    )
    const firstPushAt = failurePath.indexOf('localMessages.value.push')
    expect(guardAt).toBeGreaterThan(-1)
    expect(firstPushAt).toBeGreaterThan(guardAt)
    expect(failurePath.indexOf('creditsExhausted.value = true'))
      .toBeGreaterThan(guardAt)
  })

  it('settles the composer only while the turn still owns it', () => {
    // `ownsScreen` rather than `isCurrent`, and strictly so: see "a second
    // turn started while the first is still finishing" below for the case
    // where a turn passes the second check and fails the first.
    const settle = runChatTurn.slice(runChatTurn.indexOf('} finally {'))
    expect(settle).toContain('turnGuard.settle(ticket)')
    expect(settle).toContain('if (turnGuard.ownsScreen(ticket)) {')
  })

  it('stamps the turn before the attachment upload, not after it', () => {
    // The upload is an await like any other; a switch during it used to end
    // with the optimistic bubble landing in the next character's thread.
    const send = section('async function handleSend()', '\n/**')
    const stampAt = send.indexOf('const turn = beginChatTurn(props.character.id)')
    const uploadAt = send.indexOf('await uploadChatAttachments(')
    expect(stampAt).toBeGreaterThan(-1)
    expect(uploadAt).toBeGreaterThan(stampAt)
    expect(send).toContain('if (!turnGuard.isCurrent(turn.ticket)) {')
  })
})

describe('the one abandoned turn that was never sent', () => {
  const send = section('async function handleSend()', '\n/**')

  it('sends it anyway rather than dropping it on the floor', () => {
    // Every other abandoned turn is already on the wire — the server
    // finishes it and the reply is waiting on the way back. The upload
    // window is the exception: the request does not exist yet, so the guard
    // that returns here returns *before* anything reached the server, while
    // the composer has already been emptied and the previews revoked. The
    // player's message would simply cease to exist, with nothing to show it.
    const guardAt = send.indexOf('if (!turnGuard.isCurrent(turn.ticket)) {')
    const dispatchAt = send.indexOf('dispatchAbandonedTurn(request)')
    expect(guardAt).toBeGreaterThan(-1)
    expect(dispatchAt).toBeGreaterThan(guardAt)
  })

  it('addresses it to the thread it was written in, not the one on screen', () => {
    expect(send).toContain('character_id: turn.ticket.characterId')
    expect(send).toContain('conversation_id: turn.conversationId')
  })

  it('renders none of it — that screen belongs to somebody else now', () => {
    const dispatch = section('function dispatchAbandonedTurn(', '\n/**')
    expect(dispatch).toContain('void sendChatMessage(request)')
    // No optimistic bubble, no stream to read, no state to settle: the reply
    // lands in the database and the thread reads it back on reopen.
    expect(dispatch).not.toContain('runChatTurn')
    expect(dispatch).not.toContain('localMessages')
    expect(dispatch).not.toContain('notification.')
  })
})

describe('a second turn started while the first is still finishing', () => {
  const runChatTurn = section(
    'async function runChatTurn(', '\nasync function handleSend()',
  )

  it('leaves the single-slot screen state to the newer turn', () => {
    // The reported symptom: the first turn's `finally` runs after the second
    // has begun and blanks the tool indicator the second just lit.
    const settle = runChatTurn.slice(runChatTurn.indexOf('} finally {'))
    expect(settle).toContain('if (turnGuard.ownsScreen(ticket)) {')
    expect(settle).toContain('activeToolName.value = null')
    expect(settle).not.toContain('if (turnGuard.isCurrent(ticket)) {')
  })

  it('leaves the scene chips to it as well', () => {
    // Same slot, other end of the turn: "what you may do next" answered by
    // a turn that is no longer the latest one is answering a stale question.
    const chipsAt = runChatTurn.indexOf(
      'setStorySceneChips(reply.suggested_actions',
    )
    expect(chipsAt).toBeGreaterThan(-1)
    const before = runChatTurn.slice(0, chipsAt)
    expect(before.lastIndexOf('if (turnGuard.ownsScreen(ticket)) {'))
      .toBeGreaterThan(before.lastIndexOf("emit('conversationUpdate'"))
  })

  it('still lets the superseded turn land its reply and close its scene', () => {
    // The half that must NOT be gated. These accumulate into the thread and
    // belong there whenever they arrive; gating them (which is what bumping
    // the epoch on every `begin` would have done) throws away a reply and a
    // closing narration the player paid for.
    const pushAt = runChatTurn.indexOf(
      'localMessages.value.push(reply.assistant_message)',
    )
    const closingAt = runChatTurn.indexOf(
      'closingNarrationIndex.value = localMessages.value.length',
    )
    expect(pushAt).toBeGreaterThan(-1)
    expect(closingAt).toBeGreaterThan(pushAt)
    expect(runChatTurn.slice(pushAt, closingAt)).not.toContain('ownsScreen')
  })

  it('settles the reveal slot before handing it to the newer turn', () => {
    // One reveal slot, one resolver. Overwriting it without settling the
    // previous one strands that turn's `await` for the life of the panel —
    // its `finally` never runs, so its ticket never settles either.
    const wait = section(
      'function waitForMessageReveal(', '\nfunction handleBubbleRevealComplete',
    )
    const resolveAt = wait.indexOf('pendingRevealResolve()')
    const claimAt = wait.indexOf('pendingRevealResolve = resolve')
    expect(resolveAt).toBeGreaterThan(-1)
    expect(claimAt).toBeGreaterThan(resolveAt)
  })
})

describe('undo aims at the thread it was pressed on', () => {
  const undo = section(
    'async function handleUndoLastTurn()', '\n// Refresh the current-activity badge',
  )

  it('snapshots its target before the first await', () => {
    // Three awaits sit between the press and the last write — the confirm
    // dialog, the undo itself, the character refetch — and every write is
    // expressed relative to "the thread on screen": a trim counted back from
    // its end, a character pushed up to the parent.
    const snapshotAt = undo.indexOf('const conversationId = props.conversationId')
    const confirmAt = undo.indexOf('await confirmDialog(')
    const requestAt = undo.indexOf('await undoLastTurn(conversationId)')
    expect(snapshotAt).toBeGreaterThan(-1)
    expect(confirmAt).toBeGreaterThan(snapshotAt)
    expect(requestAt).toBeGreaterThan(confirmAt)
  })

  it('re-checks it after every one of them', () => {
    expect(undo.match(/undoTargetOnScreen\(character, conversationId\)/g)?.length)
      .toBeGreaterThanOrEqual(4)
  })

  it('refetches the rolled-back character by the snapshotted id', () => {
    expect(undo).toContain('await getCharacter(character.id)')
    expect(undo).not.toContain('props.character.id')
  })

  it('refuses to trim locally once the parent reseeded the thread', () => {
    // The variant the character check cannot catch, because the character
    // never changed: a proactive message arrives mid-undo, the parent
    // reloads the thread, and the last N bubbles are no longer the N the
    // server deleted — cutting them deletes live messages instead.
    expect(undo).toContain('const threadBefore = props.messages')
    expect(undo).toContain('const reseeded = props.messages !== threadBefore')
    const reseededAt = undo.indexOf('const reseeded =')
    const sliceAt = undo.indexOf(
      'localMessages.value.slice(0, -summary.reverted_messages)',
    )
    expect(sliceAt).toBeGreaterThan(reseededAt)
    // Nothing local can reconstruct the post-undo thread from a reseeded one.
    expect(undo).toContain("emit('conversationReloadRequested')")
  })

  it('is answered by a parent that actually reloads', () => {
    const stage = readFileSync(
      new URL('../src/pages/StagePage.vue', import.meta.url), 'utf8',
    ).replace(/\r\n/g, '\n')
    expect(stage).toContain(
      '@conversation-reload-requested="handleConversationReloadRequested"',
    )
    expect(stage).toContain('void loadHistoryFor(characterId)')
  })
})

describe('the activity badge belongs to whoever is on screen', () => {
  const refresh = section(
    'async function refreshCurrentActivity()', '\nfunction formatActivityTime',
  )

  it('re-checks the character after the snapshot comes back', () => {
    // The planner has a fast path (milliseconds) and a re-planning slow one
    // (seconds), so "the previous character's snapshot arrives after the new
    // character's" is an ordinary second-wide window, not a freak race.
    const awaitAt = refresh.indexOf('await getCurrentActivity(characterId)')
    const guardAt = refresh.indexOf('props.character?.id !== characterId', awaitAt)
    const writeAt = refresh.indexOf('currentActivity.value = snapshot.current')
    expect(awaitAt).toBeGreaterThan(-1)
    expect(guardAt).toBeGreaterThan(awaitAt)
    expect(writeAt).toBeGreaterThan(guardAt)
  })

  it('reads the id once, up front, instead of off props after the await', () => {
    expect(refresh).toContain('const characterId = props.character?.id')
    expect(refresh).toContain('const seq = ++currentActivityRequestSeq')
  })
})

describe('“still answering the previous message” has copy of its own', () => {
  it('is rendered from the catalog, not as a bare 409', () => {
    expect(panelSource).toContain('isConversationBusyError(err)')
    expect(panelSource).toContain("t('chat.conversationBusy')")
  })

  it('is translated in all three catalogs', () => {
    // Reachable far more often now that abandoning a stream leaves the
    // server finishing that turn: switch away, switch back, send.
    for (const catalog of [zhTW, enUS, jaJP]) {
      expect(typeof catalog.chat.conversationBusy).toBe('string')
      expect(catalog.chat.conversationBusy.trim().length).toBeGreaterThan(0)
    }
    expect(new Set([
      zhTW.chat.conversationBusy,
      enUS.chat.conversationBusy,
      jaJP.chat.conversationBusy,
    ]).size).toBe(3)
  })
})

/** Same hand-driven SSE body as `chatApi.test.ts`; see the note there. */
function controllableStreamResponse(): {
  response: Response
  push: (frame: string) => void
} {
  const encoder = new TextEncoder()
  let captured!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      captured = controller
    },
  })
  return {
    response: { ok: true, status: 200, body } as unknown as Response,
    push: (frame: string) => {
      try {
        captured.enqueue(encoder.encode(frame))
      } catch { /* the reader closed the stream — that is the point */ }
    },
  }
}
