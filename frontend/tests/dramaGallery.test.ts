import { describe, expect, it } from 'vitest'
import {
  buildGalleryTiles,
  createGalleryFetchGuard,
  DRAMA_SCENE_ASPECT_RATIO,
  galleryLightboxIndex,
  galleryLightboxItems,
  type DramaGalleryTile,
} from '../src/utils/dramaGallery'
import gallerySource from '../src/components/branching-drama/DramaSceneGallery.vue?raw'
import lightboxSource from '../src/components/ui/UiLightbox.vue?raw'
import pageSource from '../src/pages/BranchingDramaPage.vue?raw'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import type { CollectedScene, DramaSceneGallery } from '../src/types/branchingDrama'

function scene(overrides: Partial<CollectedScene> = {}): CollectedScene {
  return {
    node_id: 'n-1',
    title: 'Down the stairwell',
    depth: 1,
    tone: 'dark',
    image_path: 'drama-1/n-1.png',
    ...overrides,
  }
}

function gallery(overrides: Partial<DramaSceneGallery> = {}): DramaSceneGallery {
  return {
    collected: [scene()],
    locked_count: 2,
    total_with_images: 3,
    ...overrides,
  }
}

describe('buildGalleryTiles', () => {
  it('keeps the server order and appends one silhouette per locked picture', () => {
    const tiles = buildGalleryTiles(
      gallery({
        collected: [
          scene({ node_id: 'root', title: 'Opening', depth: 0, tone: null }),
          scene({ node_id: 'walked' }),
        ],
        locked_count: 2,
        total_with_images: 4,
      }),
    )

    expect(tiles.map((tile) => tile.kind)).toEqual([
      'collected', 'collected', 'locked', 'locked',
    ])
    // Silhouettes last so the collected cells stay a readable descent
    // through the story (D8.5 — the shape of the tree is drawn openly by the
    // branching graph; what a scene *is* stays off the wire).
    expect(tiles.slice(0, 2).map((tile) => tile.key)).toEqual([
      'scene:root', 'scene:walked',
    ])
  })

  it('gives every tile a distinct key so the grid does not reuse DOM nodes', () => {
    const tiles = buildGalleryTiles(
      gallery({ collected: [scene({ node_id: 'a' }), scene({ node_id: 'b' })], locked_count: 3 }),
    )

    expect(new Set(tiles.map((tile) => tile.key)).size).toBe(tiles.length)
  })

  it('resolves the scene path into a URL the image component can ask for', () => {
    const [tile] = buildGalleryTiles(
      gallery({ collected: [scene({ image_path: 'drama-1/n-1.png' })], locked_count: 0 }),
    )

    expect(tile.kind).toBe('collected')
    if (tile.kind !== 'collected') throw new Error('expected a collected tile')
    expect(tile.imageUrl).toBe('/uploads/drama-1/n-1.png')
  })

  it('passes an already-absolute scene path through untouched', () => {
    const [tile] = buildGalleryTiles(
      gallery({
        collected: [scene({ image_path: '/media/branching-dramas/d/n.png' })],
        locked_count: 0,
      }),
    )

    if (tile.kind !== 'collected') throw new Error('expected a collected tile')
    expect(tile.imageUrl).toBe('/media/branching-dramas/d/n.png')
  })

  it('carries no title, tone or picture on a locked tile', () => {
    const tiles = buildGalleryTiles(
      gallery({ collected: [], locked_count: 1, total_with_images: 1 }),
    )

    // The anti-spoiler boundary as the client sees it: there is no field on
    // a silhouette a template could render by accident.
    expect(tiles).toEqual([{ kind: 'locked', key: 'locked:0' }])
  })

  it('renders nothing rather than looping on a nonsense locked count', () => {
    expect(
      buildGalleryTiles(gallery({ collected: [], locked_count: -4 })),
    ).toEqual([])
    expect(
      buildGalleryTiles(
        gallery({ collected: [], locked_count: 2.7 }),
      ).length,
    ).toBe(2)
  })

  it('lays out an untouched drama as pure silhouettes', () => {
    const tiles = buildGalleryTiles(
      gallery({ collected: [], locked_count: 3, total_with_images: 3 }),
    )

    expect(tiles).toHaveLength(3)
    expect(tiles.every((tile) => tile.kind === 'locked')).toBe(true)
  })
})

/**
 * FX5-1. The page keeps one set of gallery refs for whichever drama is
 * selected and clears them on switch; a slow read issued against the drama
 * the player just left used to land afterwards and paint its tiles under the
 * new title. These pin the rule the page now follows.
 */
describe('createGalleryFetchGuard', () => {
  it('accepts the answer to the only request in flight', () => {
    const guard = createGalleryFetchGuard()

    const token = guard.begin()

    expect(guard.isCurrent(token)).toBe(true)
  })

  it('drops the answer to a request a newer one superseded', () => {
    const guard = createGalleryFetchGuard()

    const first = guard.begin()
    const second = guard.begin()

    expect(guard.isCurrent(first)).toBe(false)
    expect(guard.isCurrent(second)).toBe(true)
  })

  it('drops the answer to a request whose drama was left', () => {
    const guard = createGalleryFetchGuard()

    const token = guard.begin()
    guard.invalidate()

    expect(guard.isCurrent(token)).toBe(false)
  })

  it('keeps a token dead through any number of later switches', () => {
    // A very slow request can outlive several drama switches; it must stay
    // discarded, not become current again once the churn settles.
    const guard = createGalleryFetchGuard()

    const first = guard.begin()
    guard.begin()
    guard.invalidate()

    expect(guard.isCurrent(first)).toBe(false)
  })

  /**
   * The page's own shape, minimally: two overlapping opens where the first
   * resolves last. Without the guard the assertion below fails with the
   * stale drama's tiles.
   */
  it('leaves the current drama on screen when a stale read lands last', async () => {
    const guard = createGalleryFetchGuard()
    const state = { gallery: null as DramaSceneGallery | null, loading: false }
    const slow = gallery({ collected: [scene({ node_id: 'old-drama' })] })
    const fast = gallery({ collected: [scene({ node_id: 'new-drama' })] })

    async function open(
      fetch: () => Promise<DramaSceneGallery>,
    ): Promise<void> {
      const token = guard.begin()
      state.loading = true
      try {
        const next = await fetch()
        if (!guard.isCurrent(token)) return
        state.gallery = next
      } finally {
        if (guard.isCurrent(token)) state.loading = false
      }
    }

    let releaseSlow: (value: DramaSceneGallery) => void = () => {}
    const pending = new Promise<DramaSceneGallery>((resolve) => {
      releaseSlow = resolve
    })
    const stale = open(() => pending)
    await open(() => Promise.resolve(fast))
    releaseSlow(slow)
    await stale

    const collected = state.gallery?.collected ?? []
    expect(collected.map((s) => s.node_id)).toEqual(['new-drama'])
    // The superseded read must not clear the spinner of the read that
    // replaced it either — here the current read already finished, so the
    // late one arriving must leave that alone rather than re-toggling it.
    expect(state.loading).toBe(false)
  })
})

/**
 * FX5-2. Scene pictures are landscape (gateway 1536x1024, ComfyUI 1216x832)
 * while `<UiImage>`'s `thumb` / `full` plans default to the 2/3 portrait of a
 * character 立繪 — which cropped a scene thumbnail to its middle ~45% and
 * stretched it under the zoom. The ratio must stay stated, not inherited.
 */
describe('scene picture shape', () => {
  it('states a landscape ratio', () => {
    const [w, h] = DRAMA_SCENE_ASPECT_RATIO.split('/').map((part) =>
      Number(part.trim()),
    )

    expect(Number.isFinite(w) && Number.isFinite(h)).toBe(true)
    expect(w).toBeGreaterThan(h)
  })

  it('is passed to the grid thumbnail', () => {
    // 放大那一張的比例改由浮窗項目自己攜帶（見下面的 lightbox 集合測試），
    // 所以元件裡只剩縮圖這一個 `<UiImage>`。
    const uses = gallerySource.match(/<UiImage[\s\S]*?\/>/g) ?? []

    expect(uses).toHaveLength(1)
    for (const use of uses) {
      expect(use).toContain(':aspect-ratio="DRAMA_SCENE_ASPECT_RATIO"')
    }
  })

  it('rides along on every lightbox item so the placeholder box is landscape', () => {
    const items = galleryLightboxItems(
      buildGalleryTiles(gallery({ collected: [scene(), scene({ node_id: 'n-2' })] })),
    )

    expect(items).toHaveLength(2)
    for (const item of items) {
      expect(item.aspectRatio).toBe(DRAMA_SCENE_ASPECT_RATIO)
    }
  })

  it('fits the zoomed picture into that box instead of filling it', () => {
    // One box ratio serves two renderers, so the enlarged view must letterbox
    // rather than distort — `cover` there would be a visible stretch. The rule
    // moved into the shared lightbox along with the overlay (LB6); pinned here
    // because this gallery is the landscape caller that would show the stretch.
    expect(lightboxSource).toMatch(
      /\.ui-lightbox__image \{[^}]*object-fit: contain/,
    )
  })
})

/**
 * LB6. 手刻的 zoom overlay 換成共用浮窗 `UiLightbox`，玩家因此拿到左右鍵、
 * 橫滑、焦點管理、背景捲動鎖與「開原圖」。
 *
 * 這一段守的是**防劇透在導覽面的那一半**：鎖定格沒有 `imageUrl`（後端不下發），
 * 一旦被排進浮窗集合，左右鍵就會走進一頁空白，也等於把未走過的節點排成一條
 * 可枚舉的序列。SSR harness 沒有 DOM，點擊與鍵盤都跑不到，所以規則抽成純函式
 * 才有閘可守。
 */
describe('gallery lightbox set', () => {
  function tilesOf(collected: number, locked: number): DramaGalleryTile[] {
    return buildGalleryTiles(
      gallery({
        collected: Array.from({ length: collected }, (_, at) =>
          scene({
            node_id: `n-${at}`,
            title: `Scene ${at}`,
            image_path: `drama-1/n-${at}.png`,
          }),
        ),
        locked_count: locked,
      }),
    )
  }

  it('never lets a locked tile into the set', () => {
    const tiles = tilesOf(2, 3)

    const items = galleryLightboxItems(tiles)

    expect(tiles).toHaveLength(5)
    expect(items).toHaveLength(2)
    expect(items.map((item) => item.url)).toEqual([
      '/uploads/drama-1/n-0.png',
      '/uploads/drama-1/n-1.png',
    ])
    // 沒有任何一項是空的圖：空 `url` 就是鎖定格混進來的樣子。
    expect(items.every((item) => item.url !== '')).toBe(true)
  })

  it('holds nothing at all for an untouched drama', () => {
    expect(galleryLightboxItems(tilesOf(0, 4))).toEqual([])
  })

  it('captions each picture with its scene title', () => {
    const items = galleryLightboxItems(tilesOf(2, 1))

    expect(items.map((item) => item.caption)).toEqual(['Scene 0', 'Scene 1'])
  })

  it('opens the picture the player actually clicked', () => {
    const tiles = tilesOf(3, 2)

    expect(galleryLightboxIndex(tiles, 0)).toBe(0)
    expect(galleryLightboxIndex(tiles, 2)).toBe(2)
  })

  it('maps a grid position onto the collected subsequence, not onto itself', () => {
    // `buildGalleryTiles` 目前把鎖定格全排在最後，於是恆等式剛好成立——那是
    // 版面決定，不是契約。混排的網格是這個映射唯一有意義的形狀。
    const tiles: DramaGalleryTile[] = [
      { kind: 'locked', key: 'locked:0' },
      { kind: 'collected', key: 'a', nodeId: 'a', title: 'A', depth: 0, tone: null, imageUrl: '/a.png' },
      { kind: 'locked', key: 'locked:1' },
      { kind: 'locked', key: 'locked:2' },
      { kind: 'collected', key: 'b', nodeId: 'b', title: 'B', depth: 1, tone: 'dark', imageUrl: '/b.png' },
    ]

    const items = galleryLightboxItems(tiles)

    expect(items.map((item) => item.url)).toEqual(['/a.png', '/b.png'])
    expect(galleryLightboxIndex(tiles, 1)).toBe(0)
    // 第 5 格是集合裡的第 2 張。把網格索引直接當浮窗索引，這裡會開錯圖。
    expect(galleryLightboxIndex(tiles, 4)).toBe(1)
  })

  it('refuses a locked cell rather than opening the neighbour by mistake', () => {
    const tiles = tilesOf(2, 2)

    expect(galleryLightboxIndex(tiles, 2)).toBe(-1)
    expect(galleryLightboxIndex(tiles, 3)).toBe(-1)
  })

  it('refuses an index that is not a cell', () => {
    const tiles = tilesOf(2, 1)

    expect(galleryLightboxIndex(tiles, -1)).toBe(-1)
    expect(galleryLightboxIndex(tiles, 3)).toBe(-1)
    expect(galleryLightboxIndex(tiles, Number.NaN)).toBe(-1)
    expect(galleryLightboxIndex([], 0)).toBe(-1)
  })

  it('wires the grid to the shared lightbox and keeps no zoom overlay of its own', () => {
    // SSR harness 渲染不出點擊行為，所以接線是用原始碼掃描釘的。
    expect(gallerySource).toContain('<UiLightbox')
    expect(gallerySource).toContain(':items="zoomItems"')
    expect(gallerySource).toContain('@click="openTile(gridIndex)"')
    // 手刻 overlay 與它自己的 Escape 監聽必須整段消失——留著就是兩層 overlay
    // 搶同一顆鍵，外加一個沒人解綁的 listener。
    expect(gallerySource).not.toContain('drama-gallery__zoom')
    expect(gallerySource).not.toContain("addEventListener('keydown'")
  })
})

/**
 * 圖集入口鈕不得宣稱張數 (BD 計畫 D8.2 的殘留缺陷)。
 *
 * BD13–BD15 出過一次：這顆鈕用「走過的節點數」冒充「收藏的圖片張數」。兩者
 * 從來不是同一個量——圖集的張數是 `walked ∩ 有 image_path 的節點`
 * （`build_scene_gallery`），而且要等 `openGallery` 回來才知道，這顆鈕卻必須
 * 在那之前就畫出來。所以它只講完成度，一個字都不許提圖片數量。
 *
 * 目前 `frontend/tests` 沒有任何檔案引用 `entryPercent` / `entryAria`；把
 * 「已收集 {n} 張」寫回三語 catalogue，整套測試仍是綠的。這根樁補上那個缺口。
 */
describe('gallery entry button claims completion, never a picture count', () => {
  /** 圖片量詞／收藏動詞：出現任何一個就代表這顆鈕又在數張數了。 */
  const locales = [
    { name: 'zh-TW', messages: zhTW, pictureWords: /張|收集|收藏|幅|圖片/ },
    {
      name: 'en-US',
      messages: enUS,
      pictureWords: /\b(collected|collect|pictures?|images?|photos?)\b/i,
    },
    { name: 'ja-JP', messages: jaJP, pictureWords: /枚|収集|コレクション|画像/ },
  ] as const

  function galleryStrings(messages: unknown): Record<string, unknown> {
    const branchingDrama = (messages as { branchingDrama?: unknown }).branchingDrama
    return ((branchingDrama as { gallery?: Record<string, unknown> } | undefined)
      ?.gallery) ?? {}
  }

  for (const { name, messages, pictureWords } of locales) {
    const gallery = galleryStrings(messages)

    it(`${name}: states a percentage and nothing about how many pictures`, () => {
      for (const key of ['entryPercent', 'entryAria'] as const) {
        const value = gallery[key]
        expect(typeof value, `${name}.${key} must exist`).toBe('string')
        const text = value as string
        // 完成度是這顆鈕唯一被允許說的事實。
        expect(text).toContain('{percent}')
        // …而張數是它拿不到、也不該假裝拿得到的那個數字。
        expect(text).not.toMatch(pictureWords)
        // `{walked}` / `{total}` 是圖集面板 `completion` 那句的參數；它們一旦
        // 出現在入口鈕，就是「走過的節點數」又被端上來當張數的老路。
        expect(text).not.toContain('{walked}')
        expect(text).not.toContain('{total}')
      }
    })

    it(`${name}: has no entryCount key to resurrect`, () => {
      expect(Object.keys(gallery)).not.toContain('entryCount')
    })
  }

  it('the button binds entryPercent and renders no count of its own', () => {
    // 鈕的可見文字只有兩塊：`gallery.open` 的名字，與 `entryPercent`。
    const button = pageSource.slice(
      pageSource.indexOf('class="bd-page__gallery-entry"'),
      pageSource.indexOf('</button>', pageSource.indexOf('class="bd-page__gallery-entry"')),
    )
    expect(button).toContain("t('branchingDrama.gallery.entryPercent'")
    expect(button).not.toMatch(/entryCount|total_with_images|locked_count|\.length/)
  })
})
