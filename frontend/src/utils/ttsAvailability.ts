import {
  listTTSAssets,
  type TTSAssetCatalog,
} from '@/utils/api/ttsAssets'

type TTSAssetCatalogLoader = () => Promise<TTSAssetCatalog>

export async function resolveTTSAvailability(
  loadCatalog: TTSAssetCatalogLoader = listTTSAssets,
): Promise<boolean> {
  try {
    const catalog = await loadCatalog()
    return catalog.enabled && catalog.voice_presets.some(voice =>
      voice.is_complete && Boolean((voice.voice_id || voice.id).trim()),
    )
  } catch {
    return false
  }
}

interface TTSPlaybackEligibility {
  channelAvailable: boolean
  isAssistantMessage: boolean
  characterId?: string | null
  speechText: string
}

export function isTTSPlaybackEligible({
  channelAvailable,
  isAssistantMessage,
  characterId,
  speechText,
}: TTSPlaybackEligibility): boolean {
  return channelAvailable
    && isAssistantMessage
    && Boolean(characterId)
    && speechText.length > 0
}
