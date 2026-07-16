import { describe, expect, it } from 'vitest'
import {
  buildInitialRelationshipPayload,
  emptyInitialRelationshipForm,
  splitList,
} from '@/composables/useInitialRelationshipForm'

describe('initial relationship form helpers', () => {
  it('returns null for an empty form', () => {
    expect(buildInitialRelationshipPayload(emptyInitialRelationshipForm())).toBeNull()
  })

  it('builds a confirmed payload when relationship fields are present', () => {
    const form = emptyInitialRelationshipForm()
    form.relationship_label = '先從朋友開始'
    form.known_context = '玩家確認從角色卡帶入。'
    form.living_arrangement = '分開住'
    form.schedule_involvement_policy = 'invite_required'
    form.proactive_permission = true
    form.proactive_cadence_hint = '一週一兩次短訊息'

    expect(buildInitialRelationshipPayload(form)).toMatchObject({
      relationship_label: '先從朋友開始',
      known_context: '玩家確認從角色卡帶入。',
      living_arrangement: '分開住',
      schedule_involvement_policy: 'invite_required',
      proactive_permission: true,
      proactive_cadence_hint: '一週一兩次短訊息',
      confirmed_by_user: true,
    })
  })

  it('builds a payload when only living arrangement is present', () => {
    const form = emptyInitialRelationshipForm()
    form.living_arrangement = '住在一起'

    expect(buildInitialRelationshipPayload(form)).toMatchObject({
      living_arrangement: '住在一起',
      confirmed_by_user: true,
    })
  })

  it('splits comma and newline separated safe profile lists', () => {
    const form = emptyInitialRelationshipForm()
    form.profile_interests = '咖啡, 散步\n音樂'
    form.profile_life_goals = '整理作品集，練習日文'

    expect(buildInitialRelationshipPayload(form)?.safe_user_profile).toEqual({
      interests: ['咖啡', '散步', '音樂'],
      routine: '',
      life_goals: ['整理作品集', '練習日文'],
    })
    expect(splitList('A, B，C\nD')).toEqual(['A', 'B', 'C', 'D'])
  })
})
