import { describe, expect, it } from 'vitest'

import {
  nextAdminDrawerOpen,
  shouldCloseAdminDrawerOnKey,
  shouldShowAdminMobileBackLink,
} from '@/utils/adminDrawer'

describe('nextAdminDrawerOpen', () => {
  it('toggle flips closed to open', () => {
    expect(nextAdminDrawerOpen(false, 'toggle')).toBe(true)
  })

  it('toggle flips open to closed', () => {
    expect(nextAdminDrawerOpen(true, 'toggle')).toBe(false)
  })

  it('close always yields closed, even when already closed', () => {
    expect(nextAdminDrawerOpen(true, 'close')).toBe(false)
    expect(nextAdminDrawerOpen(false, 'close')).toBe(false)
  })
})

describe('shouldCloseAdminDrawerOnKey', () => {
  it('closes on Escape', () => {
    expect(shouldCloseAdminDrawerOnKey('Escape')).toBe(true)
  })

  it('leaves every other key alone', () => {
    expect(shouldCloseAdminDrawerOnKey('Enter')).toBe(false)
    expect(shouldCloseAdminDrawerOnKey('Tab')).toBe(false)
    expect(shouldCloseAdminDrawerOnKey('a')).toBe(false)
    expect(shouldCloseAdminDrawerOnKey('')).toBe(false)
  })
})

describe('shouldShowAdminMobileBackLink', () => {
  it('hides on the overview route itself', () => {
    expect(shouldShowAdminMobileBackLink('/admin')).toBe(false)
  })

  it('shows on any admin sub-page', () => {
    expect(shouldShowAdminMobileBackLink('/admin/models')).toBe(true)
    expect(shouldShowAdminMobileBackLink('/admin/characters')).toBe(true)
    expect(shouldShowAdminMobileBackLink('/admin/dev-docs/foo-spec')).toBe(true)
  })

  it('is not fooled by a path that merely starts with /admin', () => {
    // e.g. a hypothetical /admin-tools route should still count as "not
    // the overview" -- the check is on the literal '/admin' path, not a
    // prefix match, so this mostly documents the exact-equality contract.
    expect(shouldShowAdminMobileBackLink('/admin-tools')).toBe(true)
  })
})
