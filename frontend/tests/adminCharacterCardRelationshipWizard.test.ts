/**
 * IR3 — admin 匯入入口補初始關係精靈。
 *
 * `InitialRelationshipWizardModal` 原本只接在玩家側
 * `PlayerCharacterCardPanel.vue`（preview → wizard → import/install 三段
 * 式）。這張票把它接進兩個 admin 入口：
 *   - `CharactersAdminPage.vue` 的「匯入角色卡」（`.lumecard` 檔案上傳）
 *   - `CharacterCardMarketplace.vue` 的「安裝」（市集卡片一鍵安裝）
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils（見 `characterCardSource.test.ts`
 * 檔頭），沒辦法掛載元件、送出 file input change 或點按鈕再斷言 API 呼叫
 * 參數。跟 `lightbox.test.ts`／`stageNudgeSurface.test.ts` 的「原始碼掃描」
 * 段一樣，這裡直接讀原始碼文字，釘住「wizard 的 confirm 結果真的被送進
 * importCharacterCard／installCharacterCard 的 initialRelationship」這條
 * 接線，以及「略過＝送 null」「載入中不能被關掉」兩條既有慣例在 admin 側
 * 沒有被漏接。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

/** A named function's source, from its declaration to the next top-level declaration. */
function functionBody(fileSource: string, name: string, until: string): string {
  const start = fileSource.indexOf(name)
  expect(start).toBeGreaterThan(-1)
  const end = fileSource.indexOf(until, start)
  expect(end).toBeGreaterThan(start)
  return fileSource.slice(start, end)
}

const ADMIN_PAGE = source('../src/pages/admin/CharactersAdminPage.vue')
const MARKETPLACE = source('../src/components/admin/CharacterCardMarketplace.vue')
const WIZARD = source('../src/components/InitialRelationshipWizardModal.vue')
const PLAYER_PANEL = source('../src/components/PlayerCharacterCardPanel.vue')

describe('CharactersAdminPage — 匯入角色卡接上初始關係精靈', () => {
  it('掛了 InitialRelationshipWizardModal，且不是手刻的第二套表單', () => {
    expect(ADMIN_PAGE).toContain(
      "import InitialRelationshipWizardModal from '@/components/InitialRelationshipWizardModal.vue'",
    )
    expect(ADMIN_PAGE).toContain('<InitialRelationshipWizardModal')
  })

  it('選檔不再直接 import——先靜默取 preview 開精靈，而不是彈出玩家側那個預覽卡片浮窗', () => {
    const body = functionBody(ADMIN_PAGE, 'async function handleImportFile', 'async function confirmImportCard')
    expect(body).toContain('previewCharacterCard(file)')
    expect(body).toContain('relationshipWizardVisible.value = true')
    // 沒有立刻呼叫 importCharacterCard——那是 confirm 之後才做的事。
    expect(body).not.toContain('importCharacterCard(')
  })

  it('精靈 confirm 才真的呼叫 importCharacterCard，並把 payload 原樣帶成 initialRelationship', () => {
    const body = functionBody(ADMIN_PAGE, 'async function confirmImportCard', 'function closeRelationshipWizard')
    // IC2 之後多了第二個參數（建角成功才做得到的後續工作）；第一個參數的
    // 型別與語意一字未改。
    expect(body).toMatch(
      /async function confirmImportCard\(\r?\n\s*initialRelationship: InitialRelationshipPayload \| null,\r?\n\s*followUp: CharacterCreationFollowUp,\r?\n\)/,
    )
    expect(body).toContain('importCharacterCard(')
    expect(body).toContain('pendingImportFile.value,')
    expect(body).toContain('{ initialRelationship },')
  })

  it('略過精靈＝ confirm 收到 null，一路原樣送進 API（不是被吃掉或轉成 undefined）', () => {
    // confirmImportCard 的參數本身就是 wizard 的 emit('confirm', payload) 帶出來
    // 的值；wizard 的 skip() 固定 emit(null)（見下面的 wizard 區塊），這裡只確
    // 認 admin 端沒有在中途攔截、改寫、或丟掉這個 null。
    const body = functionBody(ADMIN_PAGE, 'async function confirmImportCard', 'function closeRelationshipWizard')
    expect(body).toContain('{ initialRelationship },')
    expect(body).not.toMatch(/initialRelationship\s*\?\?/)
    expect(body).not.toMatch(/initialRelationship\s*\|\|/)
  })

  it('confirm 成功後呼叫無守門的 resetRelationshipWizard——不是會被 importing 卡死的 closeRelationshipWizard', () => {
    // 成功路徑跑在 try 區塊裡，此時 importing 仍是 true（finally 還沒跑）；
    // 呼叫有守門的 closeRelationshipWizard() 會被自己的 `if (importing.value)
    // return` 擋成 no-op，精靈卡死、pending 狀態沒清（F1）。成功路徑必須走
    // 無守門的 reset。
    const body = functionBody(ADMIN_PAGE, 'async function confirmImportCard', 'function closeRelationshipWizard')
    expect(body).toContain('resetRelationshipWizard()')
    expect(body).not.toContain('closeRelationshipWizard()')
  })

  it('resetRelationshipWizard 無守門地關掉精靈並清掉 pending 檔案／preview（不留殘影給下一次開檔）', () => {
    const resetBody = functionBody(ADMIN_PAGE, 'function resetRelationshipWizard', 'function openCreateModal')
    expect(resetBody).not.toContain('if (importing.value) return')
    expect(resetBody).toContain('relationshipWizardVisible.value = false')
    expect(resetBody).toContain('pendingImportFile.value = null')
    expect(resetBody).toContain('pendingImportPreview.value = null')
  })

  it('匯入進行中不能被關掉精靈——守門的 closeRelationshipWizard 只接 @close，內部轉呼叫 reset', () => {
    const closeBody = functionBody(ADMIN_PAGE, 'function closeRelationshipWizard', 'function resetRelationshipWizard')
    expect(closeBody).toContain('if (importing.value) return')
    expect(closeBody).toContain('resetRelationshipWizard()')
    expect(ADMIN_PAGE).toContain('@close="closeRelationshipWizard"')
  })

  it('cardName／card／suggestedKnownContext 都餵給精靈，而不是空字串／null 佔位', () => {
    expect(ADMIN_PAGE).toContain(':card-name="relationshipWizardCardName"')
    expect(ADMIN_PAGE).toContain(':card="pendingImportPreview"')
    expect(ADMIN_PAGE).toContain(
      ':suggested-known-context="pendingImportPreview?.suggested_known_context ?? \'\'"',
    )
    expect(ADMIN_PAGE).toContain(':loading="importing"')
  })
})

describe('CharacterCardMarketplace — 安裝接上初始關係精靈', () => {
  it('掛了 InitialRelationshipWizardModal', () => {
    expect(MARKETPLACE).toContain(
      "import InitialRelationshipWizardModal from '@/components/InitialRelationshipWizardModal.vue'",
    )
    expect(MARKETPLACE).toContain('<InitialRelationshipWizardModal')
  })

  it('點安裝不再直接 install——先開精靈，把卡片記成 pending', () => {
    const body = functionBody(MARKETPLACE, 'function install(pack', 'async function confirmInstall')
    expect(body).toContain('pendingPack.value = pack')
    expect(body).toContain('relationshipWizardVisible.value = true')
    expect(body).not.toContain('installCharacterCard(')
  })

  it('canInstallCard 閘仍在最前面——cloud-exclusive 卡不會先開精靈才在後端被拒', () => {
    const body = functionBody(MARKETPLACE, 'function install(pack', 'async function confirmInstall')
    const guardAt = body.indexOf('if (!canInstallCard(pack)) return')
    const openAt = body.indexOf('relationshipWizardVisible.value = true')
    expect(guardAt).toBeGreaterThan(-1)
    expect(guardAt).toBeLessThan(openAt)
  })

  it('精靈 confirm 才真的呼叫 installCharacterCard，並把 payload 原樣帶成 initialRelationship', () => {
    const body = functionBody(MARKETPLACE, 'async function confirmInstall', 'function closeRelationshipWizard')
    expect(body).toMatch(
      /async function confirmInstall\(\r?\n\s*initialRelationship: InitialRelationshipPayload \| null,\r?\n\s*followUp: CharacterCreationFollowUp,\r?\n\)/,
    )
    expect(body).toContain('installCharacterCard(')
    expect(body).toContain('pack.pack_id,')
    expect(body).toContain('translate: translateOnInstall.value,')
    expect(body).toContain('initialRelationship,')
  })

  it('translate 選項沒有被精靈接線頂掉——市集既有的翻譯開關照樣送出', () => {
    const body = functionBody(MARKETPLACE, 'async function confirmInstall', 'function closeRelationshipWizard')
    const translateAt = body.indexOf('translate: translateOnInstall.value,')
    const relationshipAt = body.indexOf('initialRelationship,')
    expect(translateAt).toBeGreaterThan(-1)
    expect(relationshipAt).toBeGreaterThan(translateAt)
  })

  it('精靈 confirm 成功後呼叫無守門的 resetRelationshipWizard——不是會被 installingId 卡死的 closeRelationshipWizard', () => {
    // 成功路徑跑在 try 區塊裡，此時 installingId 仍非 null（finally 還沒
    // 跑）；呼叫有守門的 closeRelationshipWizard() 會被自己的
    // `if (installingId.value !== null) return` 擋成 no-op（F2 同型 F1）。
    const body = functionBody(MARKETPLACE, 'async function confirmInstall', 'function closeRelationshipWizard')
    expect(body).toContain('resetRelationshipWizard()')
    expect(body).not.toContain('closeRelationshipWizard()')
  })

  it('安裝進行中不能被關掉精靈——守門的 closeRelationshipWizard 只接 @close，內部轉呼叫 reset', () => {
    const closeBody = functionBody(MARKETPLACE, 'function closeRelationshipWizard', 'function resetRelationshipWizard')
    expect(closeBody).toContain('if (installingId.value !== null) return')
    expect(closeBody).toContain('resetRelationshipWizard()')
    expect(MARKETPLACE).toContain('@close="closeRelationshipWizard"')
  })

  it('resetRelationshipWizard 無守門地關掉精靈並清掉 pending 卡片', () => {
    const resetBody = functionBody(MARKETPLACE, 'function resetRelationshipWizard', 'onMounted(load)')
    expect(resetBody).not.toContain('if (installingId.value !== null) return')
    expect(resetBody).toContain('relationshipWizardVisible.value = false')
    expect(resetBody).toContain('pendingPack.value = null')
  })

  it('列表項目本身已是 CharacterCardPreview（extends），直接餵給精靈不用再打一次 preview API', () => {
    expect(MARKETPLACE).toContain(':card="pendingPack"')
    expect(MARKETPLACE).not.toContain('previewCharacterCardPack(')
  })

  it('cardName／suggestedKnownContext／loading 都接上，不是空字串佔位', () => {
    expect(MARKETPLACE).toContain(":card-name=\"pendingPack?.name || pendingPack?.title || ''\"")
    expect(MARKETPLACE).toContain(
      ':suggested-known-context="pendingPack?.suggested_known_context ?? \'\'"',
    )
    expect(MARKETPLACE).toContain(':loading="installingId !== null"')
  })
})

describe('兩個 admin 入口與玩家側共用同一顆 wizard 元件，沒有另外刻一份', () => {
  it('InitialRelationshipWizardModal.vue 沒有為了 admin 被改到破壞玩家側既有欄位', () => {
    // IR3 明確要求盡量不改 wizard 本身；這裡釘住 skip()／confirm() 的既有語意
    // （skip 固定送 null，confirm 送 payload）沒有被動過。
    //
    // IC2 在 `confirm` 後面加了第二個參數（建角成功之後才做得到的人設寫入
    // 與存卡），**第一個參數不動**——這條測試因此改成釘「第一個參數仍是
    // null／payload.value」，而不是釘整行文字。
    const skipBody = functionBody(WIZARD, 'function skip()', 'function confirm()')
    expect(skipBody).toMatch(/emit\('confirm', null[,)]/)
    const confirmBody = functionBody(WIZARD, 'function confirm()', 'function buildFollowUp')
    expect(confirmBody).toMatch(/emit\('confirm', payload\.value[,)]/)
  })

  it('玩家側三段式的 confirm 呼叫慣例仍在（比對基準，不是 admin 這次改的檔案）', () => {
    expect(PLAYER_PANEL).toContain('<InitialRelationshipWizardModal')
    expect(PLAYER_PANEL).toContain('@confirm="confirmRelationshipWizard"')
  })
})
