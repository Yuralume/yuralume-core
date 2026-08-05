/**
 * The browser half of off-screen image release (ticket IV5-C).
 *
 * Deliberately thin — every decision it makes is imported from
 * `offscreenImageRelease.ts`, which is where the tests are. What is left here
 * is bookkeeping the SSR harness cannot execute at all: creating an
 * `IntersectionObserver`, and taking targets off it again.
 *
 * Observers are shared per root margin rather than created per component. A
 * long thread renders one bubble per message and dozens carry pictures; one
 * observer watching N targets is what the API is built for, and it also means
 * every image in the thread is evaluated against the same root in the same
 * pass instead of N observers each doing their own.
 */

import {
  offscreenReleaseRootMargin,
  shouldReleaseImages,
} from './offscreenImageRelease'

/** Called whenever the element crosses the release boundary. */
export type ReleaseListener = (released: boolean) => void

interface SharedObserver {
  observer: IntersectionObserver
  listeners: Map<Element, ReleaseListener>
}

const sharedObservers = new Map<string, SharedObserver>()

function sharedObserverFor(rootMargin: string): SharedObserver | null {
  if (typeof IntersectionObserver === 'undefined') return null
  const existing = sharedObservers.get(rootMargin)
  if (existing) return existing

  const listeners = new Map<Element, ReleaseListener>()
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const listener = listeners.get(entry.target)
      if (!listener) continue
      listener(shouldReleaseImages({ intersecting: entry.isIntersecting }))
    }
  }, { rootMargin })

  const shared: SharedObserver = { observer, listeners }
  sharedObservers.set(rootMargin, shared)
  return shared
}

/**
 * Watch `el` and report whether the images inside it should be released.
 *
 * Returns the teardown. Callers must run it — the listener map holds a strong
 * reference to the element, so a bubble that unmounts without unobserving
 * would keep its whole subtree alive, which is the opposite of the point.
 *
 * With no `IntersectionObserver` the listener is told "keep them" once and
 * never again: the images stay exactly as they are today.
 */
export function observeOffscreenRelease(
  el: Element,
  marginPx: number | undefined,
  onChange: ReleaseListener,
): () => void {
  const rootMargin = offscreenReleaseRootMargin(marginPx)
  const shared = sharedObserverFor(rootMargin)
  if (!shared) {
    onChange(shouldReleaseImages({
      intersecting: false,
      observerSupported: false,
    }))
    return () => {}
  }

  shared.listeners.set(el, onChange)
  shared.observer.observe(el)

  return () => {
    shared.listeners.delete(el)
    shared.observer.unobserve(el)
  }
}
