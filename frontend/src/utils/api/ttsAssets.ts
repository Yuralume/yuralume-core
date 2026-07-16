/**
 * External TTS voice catalog. The historical endpoint name is still
 * ``/tts/assets`` for compatibility, but current deployments return
 * product-facing voice ids instead of local ref / weight files.
 */

import axios from 'axios'

export interface TTSAssetEntry {
  /** Relative-to-install-dir path. Sent to the TTS server. */
  path: string
  /** Same as ``path`` — kept for label clarity. */
  relative: string
  /** Host-side absolute path. Diagnostic only. */
  absolute_path: string
  prompt_hint: string | null
}

export interface TTSVoicePresetEntry {
  id: string
  label: string
  voice_id: string
  ref_audio_path?: string
  prompt_text?: string
  prompt_lang: string
  gpt_weights_path?: string
  sovits_weights_path?: string
  is_complete: boolean
}

export interface TTSAssetCatalog {
  enabled: boolean
  install_dir: string | null
  ref_audios: TTSAssetEntry[]
  gpt_weights: TTSAssetEntry[]
  sovits_weights: TTSAssetEntry[]
  voice_presets: TTSVoicePresetEntry[]
}

export async function listTTSAssets(): Promise<TTSAssetCatalog> {
  const res = await axios.get<TTSAssetCatalog>('/api/v1/tts/assets')
  return res.data
}
