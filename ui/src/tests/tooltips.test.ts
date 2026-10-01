/// <reference types="node" />
// Tooltip coverage across every Svelte component (see ui/src/lib/tooltip.ts):
//  - no HTML `title=` attribute is left (they are converted to data-tip; SVG <title> elements and
//    component props such as <Modal title=…> are fine)
//  - every icon-only button (class "icon", or no text content) has a data-tip, or opts out with
//    data-tip-off
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

/**
 * Files another agent (main editor) is converting in parallel, so their gaps are tolerated
 * here for now. The coordinator empties this list after merging. A listed file that has no
 * gaps left fails the last test, so the list can only shrink.
 * (gaps when written: title= attributes / icon buttons without data-tip)
 */
const ALLOWLIST: string[] = []

const SRC = fileURLToPath(new URL('..', import.meta.url))

function svelteFiles(dir: string): string[] {
  const out: string[] = []
  for (const n of readdirSync(dir)) {
    const p = join(dir, n)
    if (statSync(p).isDirectory()) {
      if (n !== 'node_modules') out.push(...svelteFiles(p))
    } else if (n.endsWith('.svelte')) out.push(p)
  }
  return out.sort()
}

/** The markup only: <script> and <style> blocks and HTML comments blanked out (line numbers kept). */
export function markupOf(src: string): string {
  const blank = (m: string) => m.replace(/[^\n]/g, ' ')
  return src
    .replace(/<script\b[\s\S]*?<\/script>/g, blank)
    .replace(/<style\b[\s\S]*?<\/style>/g, blank)
    .replace(/<!--[\s\S]*?-->/g, blank)
}

export interface Tag {
  name: string
  attrs: Map<string, string | true>
  start: number
  end: number // index just past the closing '>'
  selfClosing: boolean
}

/** Reads one start tag at `i` (which is '<'), skipping `{...}` expressions and quoted values. */
export function readTag(s: string, i: number): Tag | null {
  const m = /^<([A-Za-z][\w.:-]*)/.exec(s.slice(i, i + 80))
  if (!m) return null
  const name = m[1]
  let j = i + m[0].length
  const attrs = new Map<string, string | true>()
  while (j < s.length) {
    while (j < s.length && /\s/.test(s[j])) j++
    if (s[j] === '>') return { name, attrs, start: i, end: j + 1, selfClosing: false }
    if (s[j] === '/' && s[j + 1] === '>') return { name, attrs, start: i, end: j + 2, selfClosing: true }
    if (s[j] === '{') {
      // {...spread} or {shorthand}
      const k = skipExpr(s, j)
      const inner = s.slice(j + 1, k - 1).trim()
      attrs.set(inner.startsWith('...') ? inner : inner, true)
      j = k
      continue
    }
    const an = /^[^\s=>/]+/.exec(s.slice(j))
    if (!an) {
      j++
      continue
    }
    const key = an[0]
    j += key.length
    if (s[j] === '=') {
      j++
      let val = ''
      if (s[j] === '"' || s[j] === "'") {
        const q = s[j]
        let k = j + 1
        while (k < s.length && s[k] !== q) k = s[k] === '{' ? skipExpr(s, k) : k + 1
        val = s.slice(j + 1, k)
        j = k + 1
      } else if (s[j] === '{') {
        const k = skipExpr(s, j)
        val = s.slice(j, k)
        j = k
      } else {
        const v = /^[^\s>]+/.exec(s.slice(j))
        val = v ? v[0] : ''
        j += val.length
      }
      attrs.set(key, val)
    } else attrs.set(key, true)
  }
  return null
}

/** Index just past the `}` matching the `{` at `i` (strings and template literals skipped). */
function skipExpr(s: string, i: number): number {
  let depth = 0
  let k = i
  while (k < s.length) {
    const c = s[k]
    if (c === '"' || c === "'" || c === '`') {
      const q = c
      k++
      while (k < s.length && s[k] !== q) {
        if (s[k] === '\\') k++
        else if (q === '`' && s[k] === '$' && s[k + 1] === '{') {
          k = skipExpr(s, k + 1) - 1
        }
        k++
      }
      k++
      continue
    }
    if (c === '{') depth++
    else if (c === '}') {
      depth--
      if (depth === 0) return k + 1
    }
    k++
  }
  return s.length
}

/** Every start tag in the markup (expressions in text are skipped). */
export function tags(markup: string): Tag[] {
  const out: Tag[] = []
  let i = 0
  while (i < markup.length) {
    const c = markup[i]
    if (c === '{') {
      i = skipExpr(markup, i)
      continue
    }
    if (c === '<' && /[A-Za-z]/.test(markup[i + 1] || '')) {
      const t = readTag(markup, i)
      if (t) {
        out.push(t)
        i = t.end
        continue
      }
    }
    i++
  }
  return out
}

/** Text a person would see in a button: the content without tags, comments and block syntax. */
export function visibleText(content: string): string {
  let out = ''
  let i = 0
  while (i < content.length) {
    const c = content[i]
    if (c === '<') {
      const t = readTag(content, i)
      if (t) {
        i = t.end
        continue
      }
      const close = /^<\/[^>]*>/.exec(content.slice(i))
      if (close) {
        i += close[0].length
        continue
      }
    }
    if (c === '{') {
      const k = skipExpr(content, i)
      const inner = content.slice(i + 1, k - 1).trim()
      // {#if …} {:else} {/if} {@render …} {@html …} are not text; {expr} is
      if (!/^[#:/@]/.test(inner)) out += 'X'
      i = k
      continue
    }
    out += c
    i++
  }
  return out.replace(/&nbsp;|&[a-z]+;/g, 'X').trim()
}

const lineOf = (s: string, i: number) => s.slice(0, i).split('\n').length

interface Problem { file: string; line: number; rule: 'title' | 'button-tip'; detail: string }

function scan(file: string): Problem[] {
  const src = readFileSync(file, 'utf8')
  const markup = markupOf(src)
  const rel = relative(SRC, file)
  const problems: Problem[] = []
  for (const t of tags(markup)) {
    // lowercase = an HTML (or SVG) element; Capitalized = a component, whose title is a prop.
    // An SVG <title> element is a tag, not an attribute, so it never matches.
    const isElement = /^[a-z]/.test(t.name) && !t.name.includes(':')
    if (isElement && t.attrs.has('title')) {
      problems.push({ file: rel, line: lineOf(markup, t.start), rule: 'title', detail: `<${t.name} title=…>` })
    }
    if (t.name === 'button' && !t.selfClosing) {
      const close = markup.indexOf('</button>', t.end)
      const content = close >= 0 ? markup.slice(t.end, close) : ''
      const cls = String(t.attrs.get('class') ?? '')
      const iconOnly = /(^|[\s{'"])icon($|[\s}'"])/.test(cls) || /class:icon\b/.test([...t.attrs.keys()].join(' '))
      const noText = !visibleText(content)
      // a button with a title= is already reported by the title rule
      if ((iconOnly || noText) && !t.attrs.has('data-tip') && !t.attrs.has('data-tip-off') && !t.attrs.has('title')) {
        const label = t.attrs.get('aria-label')
        problems.push({ file: rel, line: lineOf(markup, t.start), rule: 'button-tip', detail: typeof label === 'string' ? label : visibleText(content) || '(no label)' })
      }
    }
  }
  return problems
}

const files = svelteFiles(SRC)
const found = files.flatMap(scan)
const fileOf = (p: Problem) => p.file.split(/[\\/]/).pop()!
const key = (p: Problem) => `${fileOf(p)}:${p.rule}:${p.detail}`

describe('tooltip coverage', () => {
  it.skipIf(!process.env.PRINT_TIP_GAPS)('prints the current gaps', () => {
    console.log(JSON.stringify(found.map(key), null, 2))
  })

  it('finds the component files', () => {
    expect(files.length).toBeGreaterThan(15)
  })

  it('no HTML title= attributes remain (use data-tip)', () => {
    const bad = found.filter((p) => p.rule === 'title' && !ALLOWLIST.includes(fileOf(p)))
    expect(bad.map((p) => `${p.file}:${p.line} ${p.detail}`)).toEqual([])
  })

  it('every icon-only button has a data-tip (or data-tip-off)', () => {
    const bad = found.filter((p) => p.rule === 'button-tip' && !ALLOWLIST.includes(fileOf(p)))
    expect(bad.map((p) => `${p.file}:${p.line} ${p.detail}`)).toEqual([])
  })

  it('the allowlist only lists files that still have gaps', () => {
    const live = new Set(found.map(fileOf))
    expect(ALLOWLIST.filter((f) => !live.has(f))).toEqual([])
  })
})

describe('the scanner itself', () => {
  it('reads attributes with expressions containing > and quotes', () => {
    const t = tags(`<button class="btn icon" onclick={() => a > b ? "x" : '}'} data-tip="Hi">`)[0]
    expect(t.name).toBe('button')
    expect(t.attrs.get('data-tip')).toBe('Hi')
    expect(String(t.attrs.get('class'))).toContain('icon')
  })
  it('ignores component props and script/style blocks', () => {
    const m = markupOf(`<script>const s = '<b title="x">'</script><Modal title="Settings"></Modal><style>a[title]{}</style>`)
    const hits = tags(m).filter((t) => /^[a-z]/.test(t.name) && t.attrs.has('title'))
    expect(hits).toEqual([])
  })
  it('flags an HTML title and a text-less button', () => {
    const m = `<span title="old">x</span><button aria-label="Close"><Icon name="x" /></button><button>{label}</button><button data-tip-off><Icon /></button>`
    const ts = tags(m)
    expect(ts.filter((t) => t.attrs.has('title')).map((t) => t.name)).toEqual(['span'])
    const buttons = ts.filter((t) => t.name === 'button')
    const texts = buttons.map((b) => visibleText(m.slice(b.end, m.indexOf('</button>', b.end))))
    expect(texts).toEqual(['', 'X', ''])
  })
  it('does not count block syntax as text', () => {
    expect(visibleText(`{#if a}<Icon name="check" />{:else}<Icon name="x" />{/if}`)).toBe('')
    expect(visibleText(`{#if a}Saved{/if}`)).toBe('Saved')
  })
})
