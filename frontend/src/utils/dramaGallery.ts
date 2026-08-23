import type { DramaSceneGallery } from '@/types/branchingDrama'
import type { LightboxItem } from '@/utils/lightbox'
import { resolveSceneImageUrl } from '@/utils/sceneImage'

/**
 * One cell of the 劇場圖集 grid (BD9).
 *
 * A discriminated union rather than one shape with nullable fields: a locked
 * cell has *no* title, tone or picture — not an empty one — and modelling it
 * that way is what stops a template from reaching for `tile.title` on a
 * silhouette and rendering `undefined` where a spoiler would have gone. The
 * backend already refuses to send that data; this keeps the client honest
 * about the fact that it does not have it.
 */
export type DramaGalleryTile =
  | {
      kind: 'collected'
      /** Stable `v-for` key. */
      key: string
      nodeId: string
      title: string
      depth: number
      tone: string | null
      /** Ready for `<UiImage>` — already through `resolveSceneImageUrl`. */
      imageUrl: string
    }
  | {
      kind: 'locked'
      key: string
    }

/**
 * Lay a gallery response out as grid cells: every collected picture in the
 * order the backend sent (depth, then id — a descent through the story),
 * followed by one silhouette per un-walked picture.
 *
 * ## Where the anti-spoiler line actually sits (D8.5)
 *
 * **The shape of the tree is not a secret; its contents are.** The create
 * form states the segment count, the tone vocabulary is shipped UI copy, and
 * the branching graph (`utils/dramaProgress`) now draws the whole tree
 * outright with the walked nodes lit up. Nothing is protected by making the
 * branch count hard to count.
 *
 * What stays protected is what a scene *is*: its title, its summary, its
 * picture. The backend never sends those for an un-walked node — a
 * silhouette arrives as a bare `locked_count` — and that omission, not this
 * layout, is the boundary.
 *
 * Silhouettes still come last, now for a plain reading reason: the collected
 * cells are a descent through a story, and interleaving unpainted cells
 * through them turns a sequence into rubble. Kept as-is because the ordering
 * was never doing security work that changing it would undo.
 */
export function buildGalleryTiles(
  gallery: DramaSceneGallery,
): DramaGalleryTile[] {
  const tiles: DramaGalleryTile[] = gallery.collected.map((scene) => ({
    kind: 'collected',
    key: `scene:${scene.node_id}`,
    nodeId: scene.node_id,
    title: scene.title,
    depth: scene.depth,
    tone: scene.tone,
    imageUrl: resolveSceneImageUrl(scene.image_path),
  }))
  // `locked_count` is server-authored and could in principle arrive absurd
  // or negative; a bad number must not hang the render loop.
  const locked = Math.max(0, Math.trunc(gallery.locked_count || 0))
  for (let index = 0; index < locked; index += 1) {
    tiles.push({ kind: 'locked', key: `locked:${index}` })
  }
  return tiles
}

/**
 * The shape of a branching-drama scene picture, as the renderers produce it.
 *
 * Landscape, unlike everything else this product paints: the gateway path
 * renders 1536x1024 and the local ComfyUI path 1216x832. `<UiImage>`'s
 * `thumb` / `full` plans both default to the portrait `2 / 3` of a character
 * portrait, which crops a scene down to its middle ~45% in the grid and
 * stretches it under the zoom — so every gallery `<UiImage>` states this
 * ratio explicitly rather than inheriting the variant default.
 *
 * A single ratio for two slightly different sources (1.5 vs ~1.46) is
 * deliberate: it is a *box* ratio, and the pixels are fitted into it by
 * `object-fit` (cover in the grid, contain under the zoom), so neither source
 * is ever distorted.
 */
export const DRAMA_SCENE_ASPECT_RATIO = '3 / 2'

/**
 * 圖集的放大集合（LB6）——**只有已收集的格子**。
 *
 * 這是防劇透在「導覽」這一面的守法。鎖定格沒有 `imageUrl` 可以放大（後端根本
 * 沒下發），所以問題不是「點得開嗎」而是「左右鍵會不會走進去」：一格空白的
 * 浮窗頁面雖然沒有洩漏標題或圖，卻等於把鎖定節點排進了一條可枚舉的序列。
 * 集合裡從頭到尾沒有它們，左右鍵便自然跳過。
 *
 * 每個項目自己帶 `aspectRatio`：場景圖是橫式（gateway 1536x1024、ComfyUI
 * 1216x832），而 `<UiImage>` 的 `full` 預設是直式立繪的 `2 / 3`——不說清楚，
 * 載入前的佔位框就是錯的形狀。理由與 `DRAMA_SCENE_ASPECT_RATIO` 同源。
 */
export function galleryLightboxItems(
  tiles: readonly DramaGalleryTile[],
): LightboxItem[] {
  const items: LightboxItem[] = []
  for (const tile of tiles) {
    if (tile.kind !== 'collected') continue
    items.push({
      url: tile.imageUrl,
      caption: tile.title,
      aspectRatio: DRAMA_SCENE_ASPECT_RATIO,
    })
  }
  return items
}

/**
 * 網格座標 → 浮窗座標。找不到（鎖定格、越界）回 `-1`。
 *
 * 兩個座標系不是同一個：畫面上的網格是「已收集與鎖定」混排，浮窗的集合只有
 * 前者的子序列（見 `galleryLightboxItems`）。點第 N 格要開在第 M 張，M 是
 * 「N 之前有幾格是已收集的」——直接把 N 當成浮窗索引，只要清單裡曾經出現過
 * 一格鎖定在前，玩家點的圖跟開出來的圖就是兩張。
 *
 * 目前 `buildGalleryTiles` 把鎖定格全部排在最後，於是這個映射對每一個可點的
 * 格子都剛好等於恆等式。**刻意不依賴這件事**：排序是版面決定（見該函式註解
 * 明說它「不再做安全工作」），哪天改成混排，恆等式會無聲地開錯圖。
 */
export function galleryLightboxIndex(
  tiles: readonly DramaGalleryTile[],
  gridIndex: number,
): number {
  if (!Number.isFinite(gridIndex)) return -1
  const at = Math.trunc(gridIndex)
  if (at < 0 || at >= tiles.length) return -1
  if (tiles[at]?.kind !== 'collected') return -1
  let collectedBefore = 0
  for (let cursor = 0; cursor < at; cursor += 1) {
    if (tiles[cursor]?.kind === 'collected') collectedBefore += 1
  }
  return collectedBefore
}

/**
 * Late-arriving gallery responses must not paint the drama that is on screen
 * now (FX5-1).
 *
 * The fetch lives on the page, which keeps exactly one set of gallery refs
 * for whichever drama is selected; switching dramas clears them. A slow
 * request issued against the drama the player just left still resolves, and
 * without this it writes that drama's tiles into the cleared state — the
 * player sees someone else's pictures under the current title.
 *
 * Same shape as `useCloudCredits`'s generation guard: every request carries
 * the token it was issued under and is dropped whole on arrival if anything
 * has begun or invalidated since. Extracted here rather than inlined in the
 * page so the rule is testable without a DOM.
 */
export function createGalleryFetchGuard() {
  let current = 0
  return {
    /** Open a request. Supersedes anything already in the air. */
    begin(): number {
      current += 1
      return current
    },
    /** Whether `token`'s request is still the one whose answer is wanted. */
    isCurrent(token: number): boolean {
      return token === current
    },
    /** Abandon whatever is in flight — the drama it belonged to is gone. */
    invalidate(): void {
      current += 1
    },
  }
}

export type GalleryFetchGuard = ReturnType<typeof createGalleryFetchGuard>

// The old 「已收集 X/Y」 reading (`galleryProgress`) is retired (D8.2). Its
// denominator was `total_with_images` — the nodes of the *whole* tree that
// lazy generation has already painted, walked or not — which runs *ahead* of
// the player, because the prefetch paints branches nobody ever enters. That
// made the ratio both low and flat — pinned near 1/3 only at
// `KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH=1`, down in the 1/9–15% band at the
// default depth of 2 (six beats in: walked 6, painted ≈40) — and never equal
// to what the player walked under any setting, so it said nothing about how
// much of the drama was left.
// `utils/dramaProgress.dramaCompletion` measures against the whole tree
// instead.
