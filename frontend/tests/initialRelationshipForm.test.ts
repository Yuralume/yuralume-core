import { describe, expect, it } from 'vitest'
import {
  buildInitialRelationshipEditPatch,
  buildInitialRelationshipPayload,
  emptyInitialRelationshipEditForm,
  emptyInitialRelationshipForm,
  newCharacterInitialRelationshipForm,
  splitList,
  type InitialRelationshipEditForm,
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

// ----------------------------------------------------------------------
// TR2-B: 「可以主動找我」在創角流程預設打開（opt-out），但只在創角流程。
// ----------------------------------------------------------------------

describe('create-time initial relationship defaults', () => {
  it('pre-checks the proactive permission for a new character', () => {
    expect(newCharacterInitialRelationshipForm().proactive_permission).toBe(true)
  })

  it('sends the permission even when the player fills in nothing else', () => {
    // 這是預設開的整個重點：什麼都沒填也要送出 seed，否則翻預設等於沒翻。
    expect(buildInitialRelationshipPayload(newCharacterInitialRelationshipForm()))
      .toMatchObject({ proactive_permission: true, confirmed_by_user: true })
  })

  it('writes false when the player unchecks it', () => {
    const form = newCharacterInitialRelationshipForm()
    form.proactive_permission = false

    // 全空 + 取消勾選 ⇒ 完全沒有 seed，跟翻預設前的「什麼都沒設定」一樣。
    expect(buildInitialRelationshipPayload(form)).toBeNull()
  })

  it('writes false alongside the rest when the player unchecks it but fills the form', () => {
    const form = newCharacterInitialRelationshipForm()
    form.proactive_permission = false
    form.relationship_label = '第一次見面'

    expect(buildInitialRelationshipPayload(form)).toMatchObject({
      relationship_label: '第一次見面',
      proactive_permission: false,
    })
  })

  it('leaves the neutral empty form alone so existing seeds are never backfilled', () => {
    // 後編輯路徑（IR2）載入不到 seed 時用的是這份；被創角預設污染就等於
    // 存量角色被回填成「允許」，那是拍板紅線。
    expect(emptyInitialRelationshipForm().proactive_permission).toBe(false)
    expect(emptyInitialRelationshipEditForm().proactive_permission).toBe(false)
  })

  it('only differs from the empty form by the proactive permission', () => {
    expect(newCharacterInitialRelationshipForm()).toEqual({
      ...emptyInitialRelationshipForm(),
      proactive_permission: true,
    })
  })
})

// ----------------------------------------------------------------------
// The post-creation editor (IR2): diffing against what was actually
// loaded, not against a blank form, so "didn't touch this field" and
// "cleared this field" never collapse into the same PATCH body.
// ----------------------------------------------------------------------

describe('initial relationship edit form helpers', () => {
  it('drops the create-only safe-profile fields the PATCH endpoint cannot accept', () => {
    const editForm = emptyInitialRelationshipEditForm()

    expect(editForm).not.toHaveProperty('profile_interests')
    expect(editForm).not.toHaveProperty('profile_routine')
    expect(editForm).not.toHaveProperty('profile_life_goals')
    expect(editForm.schedule_involvement_policy).toBe('none')
    expect(editForm.proactive_permission).toBe(false)
  })

  it('returns null when nothing in the form changed', () => {
    const loaded: InitialRelationshipEditForm = {
      ...emptyInitialRelationshipEditForm(),
      relationship_label: '朋友',
      user_address_name: '阿丹',
    }

    expect(buildInitialRelationshipEditPatch(loaded, { ...loaded })).toBeNull()
  })

  it('trims whitespace-only edits down to "no change"', () => {
    const loaded: InitialRelationshipEditForm = {
      ...emptyInitialRelationshipEditForm(),
      relationship_label: '朋友',
    }
    const current = { ...loaded, relationship_label: '  朋友  ' }

    expect(buildInitialRelationshipEditPatch(loaded, current)).toBeNull()
  })

  it('sends only the fields that actually changed, trimmed', () => {
    const loaded: InitialRelationshipEditForm = {
      ...emptyInitialRelationshipEditForm(),
      relationship_label: '朋友',
      tone_distance: '有點距離',
      user_profile_notes: '喜歡貓',
    }
    const current: InitialRelationshipEditForm = {
      ...loaded,
      tone_distance: '  更親近一點  ',
    }

    expect(buildInitialRelationshipEditPatch(loaded, current)).toEqual({
      tone_distance: '更親近一點',
    })
  })

  it('sends an empty string for a field the player cleared, never for an untouched one', () => {
    const loaded: InitialRelationshipEditForm = {
      ...emptyInitialRelationshipEditForm(),
      relationship_label: '朋友',
      known_context: '在同一間咖啡店認識',
    }
    const current: InitialRelationshipEditForm = {
      ...loaded,
      known_context: '',
    }

    const patch = buildInitialRelationshipEditPatch(loaded, current)
    expect(patch).toEqual({ known_context: '' })
    expect(patch).not.toHaveProperty('relationship_label')
  })

  it('includes a changed schedule policy even when it reverts to "none"', () => {
    const loaded: InitialRelationshipEditForm = {
      ...emptyInitialRelationshipEditForm(),
      schedule_involvement_policy: 'invite_required',
    }
    const current: InitialRelationshipEditForm = {
      ...loaded,
      schedule_involvement_policy: 'none',
    }

    expect(buildInitialRelationshipEditPatch(loaded, current)).toEqual({
      schedule_involvement_policy: 'none',
    })
  })

  it('includes a flipped proactive_permission as a boolean, not a string', () => {
    const loaded = emptyInitialRelationshipEditForm()
    const current: InitialRelationshipEditForm = { ...loaded, proactive_permission: true }

    expect(buildInitialRelationshipEditPatch(loaded, current)).toEqual({
      proactive_permission: true,
    })
  })

  it('folds several simultaneous edits into one patch, including the two address names', () => {
    const loaded = emptyInitialRelationshipEditForm()
    const current: InitialRelationshipEditForm = {
      ...loaded,
      user_address_name: '小夏',
      character_address_name: '前輩',
      proactive_permission: true,
      proactive_cadence_hint: '一天最多一次',
    }

    expect(buildInitialRelationshipEditPatch(loaded, current)).toEqual({
      user_address_name: '小夏',
      character_address_name: '前輩',
      proactive_permission: true,
      proactive_cadence_hint: '一天最多一次',
    })
  })
})
