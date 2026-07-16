import type { PeerProfileSeedPayload } from '@/utils/api/characters'

export interface PeerProfileSeedFormValues {
  summary: string
  occupation: string
  haunts: string
  habits: string
  relationshipNote: string
  sharedActivities: string
}

export function buildPeerProfileSeedPayload(
  values: PeerProfileSeedFormValues,
): PeerProfileSeedPayload | null {
  const haunts = splitPeerProfileSeedList(values.haunts)
  const habits = splitPeerProfileSeedList(values.habits)
  const sharedActivities = splitPeerProfileSeedList(values.sharedActivities)
  const payload: PeerProfileSeedPayload = {
    summary: values.summary.trim(),
    occupation: values.occupation.trim(),
    haunts,
    habits,
    relationship_note: values.relationshipNote.trim(),
    shared_activities: sharedActivities,
  }
  const hasMaterial = Boolean(
    payload.summary ||
      payload.occupation ||
      payload.relationship_note ||
      haunts.length ||
      habits.length ||
      sharedActivities.length,
  )
  return hasMaterial ? payload : null
}

export function splitPeerProfileSeedList(value: string): string[] {
  return value
    .split(/[,，、\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
}
