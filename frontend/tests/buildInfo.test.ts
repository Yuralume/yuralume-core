import { describe, expect, it } from 'vitest'

import { buildInfoTitle, formatBuildVersion } from '@/utils/buildInfo'
import type { BuildInfo } from '@/utils/api/auth'

function buildInfo(overrides: Partial<BuildInfo> = {}): BuildInfo {
  return {
    name: 'Yuralume Core',
    version: '0.1.0',
    api_version: 'v1',
    build: {
      image_tag: 'v0.1.0',
      commit_sha: 'abcdef123456',
      built_at: '2026-06-14T12:00:00Z',
    },
    ...overrides,
  }
}

describe('formatBuildVersion', () => {
  it('shows the Core version and short commit when available', () => {
    expect(formatBuildVersion(buildInfo())).toBe('Core v0.1.0 - abcdef1')
  })

  it('falls back to version only when commit sha is absent', () => {
    expect(formatBuildVersion(buildInfo({
      build: {
        image_tag: null,
        commit_sha: null,
        built_at: null,
      },
    }))).toBe('Core v0.1.0')
  })

  it('returns an empty label without build info', () => {
    expect(formatBuildVersion(null)).toBe('')
  })
})

describe('buildInfoTitle', () => {
  it('includes full metadata for hover/title inspection', () => {
    expect(buildInfoTitle(buildInfo())).toBe(
      'Yuralume Core v0.1.0 - API v1 - image v0.1.0 - commit abcdef123456 - built 2026-06-14T12:00:00Z',
    )
  })
})
