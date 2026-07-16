import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const projectRoot = path.resolve(import.meta.dirname, '..')
const srcRoot = path.join(projectRoot, 'src')

const FORMATTER_MODULE = '@/i18n/formatters'
const requiredArgsByExport = new Map([
  ['formatDateTime', 3],
  ['formatDate', 3],
  ['formatTime', 3],
  ['formatTimeRange', 4],
])

const ignoredFiles = new Set([
  'src/i18n/formatters.ts',
])

function walk(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true })
  return entries.flatMap((entry) => {
    const absolute = path.join(dir, entry.name)
    if (entry.isDirectory()) return walk(absolute)
    if (!/\.(vue|ts)$/.test(entry.name)) return []
    return [absolute]
  })
}

function relativeFromProject(absolute) {
  return path.relative(projectRoot, absolute).replaceAll(path.sep, '/')
}

function importedFormatterNames(source) {
  const names = new Map()
  const importPattern = new RegExp(
    String.raw`import\s*\{([\s\S]*?)\}\s*from\s*['"]${FORMATTER_MODULE.replaceAll('/', String.raw`\/`)}['"]`,
    'g',
  )

  for (const match of source.matchAll(importPattern)) {
    const members = match[1]
      .split(',')
      .map((raw) => raw.trim())
      .filter(Boolean)
    for (const member of members) {
      const [exportedRaw, aliasRaw] = member.split(/\s+as\s+/)
      const exported = exportedRaw.trim()
      const alias = (aliasRaw ?? exportedRaw).trim()
      if (requiredArgsByExport.has(exported)) {
        names.set(alias, {
          exported,
          requiredArgs: requiredArgsByExport.get(exported),
        })
      }
    }
  }

  return names
}

function matchingParenIndex(source, openIndex) {
  let depth = 0
  let quote = null
  let escaped = false

  for (let i = openIndex; i < source.length; i += 1) {
    const ch = source[i]

    if (quote) {
      if (escaped) {
        escaped = false
      } else if (ch === '\\') {
        escaped = true
      } else if (ch === quote) {
        quote = null
      }
      continue
    }

    if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch
      continue
    }
    if (ch === '(') depth += 1
    if (ch === ')') {
      depth -= 1
      if (depth === 0) return i
    }
  }

  return -1
}

function topLevelArgumentCount(argsSource) {
  const trimmed = argsSource.trim()
  if (!trimmed) return 0

  let depth = 0
  let quote = null
  let escaped = false
  let count = 1

  for (const ch of argsSource) {
    if (quote) {
      if (escaped) {
        escaped = false
      } else if (ch === '\\') {
        escaped = true
      } else if (ch === quote) {
        quote = null
      }
      continue
    }

    if (ch === '"' || ch === "'" || ch === '`') {
      quote = ch
      continue
    }
    if ('([{'.includes(ch)) depth += 1
    if (')]}'.includes(ch)) depth -= 1
    if (ch === ',' && depth === 0) count += 1
  }

  return count
}

function lineNumber(source, index) {
  return source.slice(0, index).split(/\r?\n/).length
}

function findViolations(relative, source, imports) {
  const violations = []
  for (const [localName, meta] of imports.entries()) {
    const callPattern = new RegExp(String.raw`\b${localName}\s*\(`, 'g')
    for (const match of source.matchAll(callPattern)) {
      const callIndex = match.index
      const prev = callIndex > 0 ? source[callIndex - 1] : ''
      if (/[\w$.]/.test(prev)) continue

      const openIndex = source.indexOf('(', callIndex)
      const closeIndex = matchingParenIndex(source, openIndex)
      if (closeIndex === -1) continue

      const argsSource = source.slice(openIndex + 1, closeIndex)
      const argCount = topLevelArgumentCount(argsSource)
      if (argCount < meta.requiredArgs) {
        violations.push(
          `${relative}:${lineNumber(source, callIndex)}: ${localName}() imports ${meta.exported} and must pass an explicit timeZone argument`,
        )
      }
    }
  }
  return violations
}

const violations = []

for (const absolute of walk(srcRoot)) {
  const relative = relativeFromProject(absolute)
  if (ignoredFiles.has(relative)) continue

  const source = fs.readFileSync(absolute, 'utf8')
  const imports = importedFormatterNames(source)
  if (imports.size === 0) continue
  violations.push(...findViolations(relative, source, imports))
}

if (violations.length > 0) {
  console.error('Timezone formatter calls missing explicit timeZone:')
  for (const line of violations) console.error(`  ${line}`)
  process.exit(1)
}

console.log('Timezone formatter check passed.')
