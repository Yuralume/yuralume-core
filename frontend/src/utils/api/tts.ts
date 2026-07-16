/**
 * TTS API wrapper — synth a chat bubble's text into a playable URL.
 *
 * The backend caches by content hash, so calling this repeatedly with
 * the same text + character returns the same URL with ``cached: true``
 * — replays are effectively free.
 */

import axios from 'axios'

export interface TTSSynthResponse {
  audio_url: string
  cached: boolean
}

export class TTSDisabledError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'TTSDisabledError'
  }
}

export async function synthesizeCharacterTTS(
  characterId: string,
  text: string,
): Promise<TTSSynthResponse> {
  try {
    const res = await axios.post<TTSSynthResponse>(
      `/api/v1/characters/${characterId}/tts`,
      { text },
    )
    return res.data
  } catch (err) {
    if (
      axios.isAxiosError(err)
      && (err.response?.status === 403 || err.response?.status === 503)
    ) {
      throw new TTSDisabledError(
        err.response.data?.detail ?? 'TTS is not configured',
      )
    }
    throw err
  }
}
