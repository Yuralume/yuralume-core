import type { BuildInfo } from '@/utils/api/auth'

export function formatBuildVersion(info: BuildInfo | null | undefined): string {
  if (!info?.version) return ''
  const commit = shortCommitSha(info.build?.commit_sha)
  return commit ? `Core v${info.version} - ${commit}` : `Core v${info.version}`
}

export function buildInfoTitle(info: BuildInfo | null | undefined): string {
  if (!info) return ''
  const parts = [
    `${info.name} v${info.version}`,
    `API ${info.api_version}`,
    info.build?.image_tag ? `image ${info.build.image_tag}` : null,
    info.build?.commit_sha ? `commit ${info.build.commit_sha}` : null,
    info.build?.built_at ? `built ${info.build.built_at}` : null,
  ].filter((part): part is string => Boolean(part))
  return parts.join(' - ')
}

function shortCommitSha(commitSha: string | null | undefined): string {
  const normalized = commitSha?.trim()
  if (!normalized) return ''
  return normalized.slice(0, 7)
}
