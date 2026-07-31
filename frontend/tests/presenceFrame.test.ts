import { describe, expect, it } from 'vitest'

import { webDmPresenceFrame, webStagePresenceFrame } from '@/types/chat'

// The presence frame is this client's half of the co-presence contract with
// the backend prompt builder (plan SA, D2). Since the same-space gate
// retired, a same-space turn is the player's own declaration: there is no
// verdict to consult and no reason to justify, so the frame must come out
// the same every time. These are the assertions that would have caught the
// frame still claiming a retired verdict value after the gate was removed —
// which would have put every same-space turn back into the "you cannot
// plausibly be here" branch on the server.

describe('webStagePresenceFrame', () => {
  it('declares same-space as the player’s own framing', () => {
    expect(webStagePresenceFrame()).toEqual({
      surface: 'web_stage',
      channel: 'kokoro_stage',
      visibility: 'virtual_same_space',
      access_context: 'player_declared',
    })
  })

  it('keeps the declaration when the turn carries pictures', () => {
    const frame = webStagePresenceFrame(true)
    expect(frame.visibility).toBe('text_and_attachments')
    expect(frame.access_context).toBe('player_declared')
  })

  it('sends only the structural enums — no retired judge fields', () => {
    expect(Object.keys(webStagePresenceFrame()).sort()).toEqual([
      'access_context',
      'channel',
      'surface',
      'visibility',
    ])
  })
})

describe('webDmPresenceFrame', () => {
  it('is unchanged by the same-space gate retiring', () => {
    expect(webDmPresenceFrame()).toEqual({
      surface: 'web_dm',
      channel: 'kokoro_dm',
      visibility: 'text_only',
      access_context: 'text_message_only',
    })
    expect(webDmPresenceFrame(true).visibility).toBe('text_and_attachments')
  })
})
