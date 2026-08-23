/**
 * The two browser globals a *module* is allowed to touch at import time.
 *
 * The harness runs in node with no DOM, which is fine for almost everything:
 * a component that only reads `window` inside a handler never runs it under
 * SSR. `useAuth` is the exception — it seeds the token ref with
 * `localStorage.getItem(...)` at module scope, so merely importing anything
 * that transitively imports it (`ActionPriceHint`, and so any surface that
 * quotes a price) throws `ReferenceError: localStorage is not defined`.
 *
 * Import this module **first**, before the modules under test: ESM evaluates
 * imports in declaration order, so the stub is in place by the time their
 * module bodies run.
 */

const values = new Map<string, string>()

const storage = {
  getItem: (key: string) => values.get(key) ?? null,
  setItem: (key: string, value: string) => {
    values.set(key, value)
  },
  removeItem: (key: string) => {
    values.delete(key)
  },
  clear: () => {
    values.clear()
  },
  key: (index: number) => [...values.keys()][index] ?? null,
  get length() {
    return values.size
  },
} as Storage

const globals = globalThis as { localStorage?: Storage }
if (!globals.localStorage) globals.localStorage = storage

export { storage as testLocalStorage }
