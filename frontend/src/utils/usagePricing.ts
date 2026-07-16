/**
 * Client-side cost estimation for the observability usage report.
 *
 * The backend stores a catalog-derived ``cost_amount`` per event, but
 * operators want to plug in their own per-model API prices and see the
 * total recomputed live — without editing the server-side price JSON.
 * This module keeps that math and its localStorage persistence as pure,
 * testable functions; the reactive wiring lives in
 * ``UsageCostCalculator.vue``.
 *
 * Prices are entered per **1,000,000 units** of the metered quantity
 * (tokens for LLM), matching how providers publish API pricing.
 */

const PER_MILLION = 1_000_000

export const PRICE_BOOK_STORAGE_KEY = 'yuralume.usagePriceBook.v1'

/** One editable price row, entered per 1M input / output units. Kept as
 * strings so a half-typed field (``""`` / ``"1."``) round-trips without
 * being coerced to ``0`` mid-edit. */
export interface PriceEntry {
  inputPerMillion: string
  outputPerMillion: string
}

export type PriceBook = Record<string, PriceEntry>

/** Stable key for a (capability, provider, model) triple. Capability is
 * included because the same model id can be priced differently per
 * capability (e.g. text vs image). Uses a unit-separator so it can't
 * collide with real ids. */
export function priceBookKey(
  capability: string,
  providerId: string,
  modelId: string,
): string {
  return [capability, providerId, modelId].join('␟')
}

function parseAmount(raw: string): number {
  if (!raw) return 0
  const value = Number(raw)
  return Number.isFinite(value) && value > 0 ? value : 0
}

/** ``true`` when the operator has entered at least one positive price. */
export function hasPrice(entry: PriceEntry | undefined): boolean {
  if (!entry) return false
  return parseAmount(entry.inputPerMillion) > 0 || parseAmount(entry.outputPerMillion) > 0
}

/**
 * Cost = input_qty × input_price/1M + output_qty × output_price/1M.
 * Blank / non-positive prices contribute nothing, so a partially-priced
 * row still yields a usable estimate.
 */
export function computeCustomCost(
  inputQuantity: number,
  outputQuantity: number,
  entry: PriceEntry | undefined,
): number {
  if (!entry) return 0
  const inputRate = parseAmount(entry.inputPerMillion)
  const outputRate = parseAmount(entry.outputPerMillion)
  return (
    (inputQuantity * inputRate) / PER_MILLION
    + (outputQuantity * outputRate) / PER_MILLION
  )
}

function isPriceEntry(value: unknown): value is PriceEntry {
  return (
    typeof value === 'object'
    && value !== null
    && typeof (value as PriceEntry).inputPerMillion === 'string'
    && typeof (value as PriceEntry).outputPerMillion === 'string'
  )
}

/** Load the persisted price book, tolerating missing / corrupt storage. */
export function loadPriceBook(
  storage: Pick<Storage, 'getItem'> | null | undefined,
): PriceBook {
  if (!storage) return {}
  try {
    const raw = storage.getItem(PRICE_BOOK_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (typeof parsed !== 'object' || parsed === null) return {}
    const book: PriceBook = {}
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (isPriceEntry(value)) book[key] = value
    }
    return book
  } catch {
    return {}
  }
}

/** Persist the price book, swallowing quota / disabled-storage errors. */
export function savePriceBook(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  book: PriceBook,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(PRICE_BOOK_STORAGE_KEY, JSON.stringify(book))
    return true
  } catch {
    return false
  }
}
