import { describe, expect, it } from 'vitest'
import {
  buildPeerProfileSeedPayload,
  splitPeerProfileSeedList,
} from '@/utils/peerProfileSeed'

describe('peer profile seed helpers', () => {
  it('returns null when no seed material is present', () => {
    expect(buildPeerProfileSeedPayload({
      summary: ' ',
      occupation: '',
      haunts: '',
      habits: '',
      relationshipNote: '',
      sharedActivities: '',
    })).toBeNull()
  })

  it('trims flat fields and splits list fields', () => {
    expect(buildPeerProfileSeedPayload({
      summary: '  小英在神社打工  ',
      occupation: '  神社巫女 ',
      haunts: '神社, 商店街\n車站',
      habits: '下班後回訊息、整理籤詩',
      relationshipNote: '  小蘭常找她聊天 ',
      sharedActivities: '散步，喝咖啡',
    })).toEqual({
      summary: '小英在神社打工',
      occupation: '神社巫女',
      haunts: ['神社', '商店街', '車站'],
      habits: ['下班後回訊息', '整理籤詩'],
      relationship_note: '小蘭常找她聊天',
      shared_activities: ['散步', '喝咖啡'],
    })
  })

  it('splits comma, ideographic comma, and newline separated values', () => {
    expect(splitPeerProfileSeedList('A, B，C、D\nE')).toEqual(['A', 'B', 'C', 'D', 'E'])
  })
})
