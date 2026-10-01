// The layout engine. One engine produces the text layer for both the preview and the
// saved file, so line breaks and positions cannot differ between them.
//
// Everything is computed in OUTPUT pixels. The result is plain JSON and
// deterministic for a given input and font files.

import { parseMarkup, plainText, type Seg } from './markup'
import { availableWeight, family, hasSmallCaps, missingGlyphs, onFontsChanged, resolveFont, stack } from './fonts'
import { applyTextSettings, measureDrawn } from './render'
import { DEFAULT_PRINT_WIDTH_IN, scaleFor } from './units'
import type { Block, LayoutResult, Rect, Run, Template, TextStyle, Warning } from './types'

export interface Measurer {
  /** Advance width of `text` exactly as drawRuns draws it: same font, kerning, small caps
   * and letter spacing `ls` (px, after every character). */
  width(text: string, font: string, smallCaps: boolean, ls?: number): number
  metrics(font: string): { ascent: number; descent: number }
}

export interface LayoutInput {
  template: Template
  sourceRect: Rect // photo region in the upright source (px)
  /** horizontal DPI (upright), used for widths: side borders, side padding, insets */
  dpi: number | null
  /** vertical DPI (upright), used for heights and font sizes; defaults to `dpi` */
  dpiY?: number | null
  texts: Record<string, string> // markup per block id
  mode: 'band' | 'erase'
  erase?: { canvas: [number, number]; photoRect: Rect; bandRect: Rect; bandColor: string }
  gray?: boolean
  measurer?: Measurer
}

// ---------------------------------------------------------------------------
// canvas measurer (browser)

let _ctx: CanvasRenderingContext2D | null = null
const _cache = new Map<string, number>()
const _metrics = new Map<string, { ascent: number; descent: number }>()

export const canvasMeasurer: Measurer = {
  width(text, font, smallCaps, ls = 0) {
    const key = font + '|' + (smallCaps ? 1 : 0) + '|' + ls + '|' + text
    let w = _cache.get(key)
    if (w !== undefined) return w
    if (!_ctx) _ctx = document.createElement('canvas').getContext('2d')!
    // the drawer's settings (kerning, small caps, letter spacing, rendering mode), so measure == draw
    applyTextSettings(_ctx, font, smallCaps, ls)
    w = measureDrawn(_ctx, text, ls)
    if (_cache.size > 20000) _cache.clear()
    _cache.set(key, w)
    return w
  },
  metrics(font) {
    let m = _metrics.get(font)
    if (m) return m
    if (!_ctx) _ctx = document.createElement('canvas').getContext('2d')!
    applyTextSettings(_ctx, font, false, 0)
    const tm = _ctx.measureText('Hg')
    const size = parseFloat(/(\d+(?:\.\d+)?)px/.exec(font)?.[1] || '16')
    m = {
      ascent: tm.fontBoundingBoxAscent ?? size * 0.8,
      descent: tm.fontBoundingBoxDescent ?? size * 0.2,
    }
    _metrics.set(font, m)
    return m
  },
}

export function clearMeasureCache() {
  _cache.clear()
  _metrics.clear()
}

// widths measured before a font finished loading are wrong: drop them when fonts change
onFontsChanged(clearMeasureCache)

// ---------------------------------------------------------------------------

interface Frag {
  text: string
  bold: boolean
  italic: boolean
  w: number
}
interface Unit {
  frags: Frag[]
  w: number
  spaceW: number // width of the space before this unit (0 if glued, e.g. after a hyphen)
  glued: boolean
  softHyphen: boolean // a break here shows a hyphen
}
interface LineFrag {
  text: string
  bold: boolean
  italic: boolean
  x: number
  w: number
  space?: boolean // a stretchable gap (justified lines only)
}
interface Line {
  // non-justified lines: one frag per run as drawn (same-font pieces merged, measured as merged)
  // justified lines: one frag per word piece, plus space frags between words
  frags: LineFrag[]
  w: number
  last: boolean // last line of its paragraph
  justify: boolean
}

interface FontChoice {
  font: string
  weight: number
  italic: boolean
  smallCaps: boolean
}

interface BlockLayout {
  block: Block
  style: TextStyle
  sizePx: number // size at scale 1 (already clamped to the minimum)
  minPx: number // 4 pt at the file's DPI (0 when there is no DPI)
  size: number // current size (after shrink and clamp)
  lineH: number
  lines: Line[]
  before: number
  after: number
  fontFor: (bold: boolean, italic: boolean) => FontChoice
  familyStack: string
  fontId: string
  ls: number
  wantSmallCaps: boolean
  asc: number
  desc: number
}

const SOFT = '­'
const SOFT_RE = /­/g

function transformCase(text: string, c: TextStyle['case']): string {
  if (c === 'upper') return text.toUpperCase()
  if (c === 'lower') return text.toLowerCase()
  return text
}

function layoutBlock(bl: BlockLayout, markup: string, maxW: number, meas: Measurer): Line[] {
  const paras = parseMarkup(transformCaseMarkup(markup, bl.style.case))
  const lines: Line[] = []
  const wOf = (text: string, bold: boolean, italic: boolean) => {
    const ff = bl.fontFor(bold, italic)
    return meas.width(text.replace(SOFT_RE, ''), ff.font, ff.smallCaps, bl.ls)
  }
  const fontKey = (bold: boolean, italic: boolean) => {
    const ff = bl.fontFor(bold, italic)
    return ff.font + '|' + ff.smallCaps
  }
  const justifyAlign = bl.style.align === 'justify'

  /** The line exactly as it will be drawn, measured the way it is drawn. */
  const buildLine = (us: Unit[], last: boolean, hyphenate: boolean): Line => {
    const pieces: { text: string; bold: boolean; italic: boolean; space: boolean }[] = []
    us.forEach((u, i) => {
      if (i > 0 && !u.glued) {
        // the space takes the style of the previous fragment
        const prev = pieces[pieces.length - 1]
        pieces.push({ text: ' ', bold: prev?.bold ?? false, italic: prev?.italic ?? false, space: true })
      }
      u.frags.forEach((f, j) => {
        let text = f.text.replace(SOFT_RE, '')
        if (hyphenate && i === us.length - 1 && j === u.frags.length - 1) text += '-'
        if (text) pieces.push({ text, bold: f.bold, italic: f.italic, space: false })
      })
    })
    const justify = justifyAlign && !last
    const frags: LineFrag[] = []
    let x = 0
    if (justify) {
      // each piece is drawn (and so measured) on its own; spaces become stretchable gaps
      for (const p of pieces) {
        const w = wOf(p.text, p.bold, p.italic)
        frags.push({ text: p.text, bold: p.bold, italic: p.italic, x, w, space: p.space || undefined })
        x += w
      }
    } else {
      // merge consecutive pieces drawn with the same font into one run, and measure the run's
      // own text (kerning/ligatures across the merged pieces are then what the drawer gets)
      const groups: { text: string; bold: boolean; italic: boolean; key: string }[] = []
      for (const p of pieces) {
        const k = fontKey(p.bold, p.italic)
        const g = groups[groups.length - 1]
        if (g && g.key === k) g.text += p.text
        else groups.push({ text: p.text, bold: p.bold, italic: p.italic, key: k })
      }
      for (const g of groups) {
        const adv = wOf(g.text, g.bold, g.italic)
        const trimmed = g.text.replace(/ +$/, '')
        if (trimmed) {
          const w = trimmed === g.text ? adv : wOf(trimmed, g.bold, g.italic)
          frags.push({ text: trimmed, bold: g.bold, italic: g.italic, x, w })
        }
        x += adv
      }
    }
    return { frags, w: x, last, justify }
  }

  for (const para of paras) {
    const units = toUnits(para, wOf)
    if (!units.length) {
      lines.push({ frags: [], w: 0, last: true, justify: false })
      continue
    }
    const queue = units.slice()
    let cur: Unit[] = []
    let curW = 0
    /** Emit `cur` as a line broken before `next`. The fit test sums word widths; the real line
     * (merged runs, plus the hyphen when breaking at a soft hyphen) can be a little wider, so
     * units are handed back from the end until it fits. Returns the handed-back units. */
    const flush = (next: Unit | null): Unit[] => {
      const carry: Unit[] = []
      for (;;) {
        const nx = carry[0] ?? next
        const hy = !!nx && cur[cur.length - 1].softHyphen && nx.glued
        const line = buildLine(cur, !nx, hy)
        if (line.w <= maxW + 0.01 || cur.length === 1) {
          lines.push(line)
          break
        }
        carry.unshift(cur.pop()!)
      }
      cur = []
      curW = 0
      return carry
    }
    while (queue.length || cur.length) {
      if (!queue.length) {
        queue.push(...flush(null))
        continue
      }
      const u = queue.shift()!
      if (!cur.length) {
        if (u.w > maxW + 0.01) {
          // a word wider than the line: break by character
          const pieces = breakByChar(u, maxW, wOf)
          for (let k = 0; k < pieces.length - 1; k++) lines.push(buildLine([pieces[k]], false, false))
          cur = [pieces[pieces.length - 1]]
          curW = cur[0].w
        } else {
          cur = [u]
          curW = u.w
        }
        continue
      }
      const add = (u.glued ? 0 : u.spaceW) + u.w
      if (curW + add <= maxW + 0.01) {
        cur.push(u)
        curW += add
        continue
      }
      queue.unshift(...flush(u), u)
    }
  }
  return lines
}

function transformCaseMarkup(markup: string, c: TextStyle['case']): string {
  if (c !== 'upper' && c !== 'lower') return markup
  // escapes (\*, \\) are unaffected by case changes
  return transformCase(markup, c)
}

function toUnits(para: Seg[], wOf: (t: string, b: boolean, i: boolean) => number): Unit[] {
  // split into words (non-space runs, may span style changes), then chunks at hyphens / soft hyphens
  const words: Frag[][] = []
  let cur: Frag[] = []
  let gapBefore: boolean[] = []
  let sawSpace = false
  for (const seg of para) {
    const parts = seg.text.split(/( +)/)
    for (const part of parts) {
      if (!part) continue
      if (/^ +$/.test(part)) {
        if (cur.length) {
          words.push(cur)
          gapBefore.push(sawSpace)
          cur = []
        }
        sawSpace = true
        continue
      }
      cur.push({ text: part, bold: seg.bold, italic: seg.italic, w: 0 })
    }
  }
  if (cur.length) {
    words.push(cur)
    gapBefore.push(sawSpace)
  }
  const units: Unit[] = []
  words.forEach((word, wi) => {
    // chunk at "-" (keep the hyphen with the left part) and at soft hyphens
    const chunks: { frags: Frag[]; soft: boolean }[] = [{ frags: [], soft: false }]
    for (const f of word) {
      const pieces = f.text.split(/(?<=[-‐­])/)
      pieces.forEach((p, k) => {
        if (k > 0) chunks.push({ frags: [], soft: false })
        chunks[chunks.length - 1].frags.push({ ...f, text: p })
        if (p.endsWith(SOFT)) chunks[chunks.length - 1].soft = true
      })
    }
    chunks.forEach((ch, ci) => {
      const frags = ch.frags.filter((f) => f.text.length)
      if (!frags.length) return
      for (const f of frags) f.w = wOf(f.text, f.bold, f.italic)
      const first = frags[0]
      const prevFrag = ci > 0 ? chunks[ci - 1].frags.at(-1) : words[wi - 1]?.at(-1)
      const spaceStyle = prevFrag ?? first
      units.push({
        frags,
        w: frags.reduce((a, f) => a + f.w, 0),
        spaceW: wOf(' ', spaceStyle.bold, spaceStyle.italic),
        glued: ci > 0 || (wi > 0 && !gapBefore[wi] ? true : false),
        softHyphen: ch.soft,
      })
    })
  })
  // the very first unit is never glued
  if (units.length) units[0].glued = false
  return units
}

function breakByChar(u: Unit, maxW: number, wOf: (t: string, b: boolean, i: boolean) => number): Unit[] {
  const out: Unit[] = []
  let cur: Frag[] = []
  let curW = 0
  for (const f of u.frags) {
    let buf = ''
    for (const ch of f.text) {
      const w = wOf(buf + ch, f.bold, f.italic)
      if (curW + w > maxW && (buf || cur.length)) {
        if (buf) cur.push({ ...f, text: buf, w: wOf(buf, f.bold, f.italic) })
        out.push({ frags: cur, w: cur.reduce((a, x) => a + x.w, 0), spaceW: 0, glued: false, softHyphen: false })
        cur = []
        curW = 0
        buf = ch
      } else buf += ch
    }
    if (buf) {
      const w = wOf(buf, f.bold, f.italic)
      cur.push({ ...f, text: buf, w })
      curW += w
    }
  }
  if (cur.length) out.push({ frags: cur, w: cur.reduce((a, x) => a + x.w, 0), spaceW: 0, glued: false, softHyphen: false })
  out[0].spaceW = u.spaceW
  out[0].glued = u.glued
  return out
}

// ---------------------------------------------------------------------------

export function effectiveTemplate(t: Template, overrides?: { layout?: any; blocks?: Record<string, any>; scaleMode?: any } | null): Template {
  const clone: Template = JSON.parse(JSON.stringify(t))
  if (!overrides) return clone
  if (overrides.scaleMode) clone.scaleMode = overrides.scaleMode
  if (overrides.layout) deepAssign(clone.layout, overrides.layout)
  if (overrides.blocks) {
    for (const b of clone.blocks) {
      const o = overrides.blocks[b.id]
      if (o) {
        const { column, ...style } = o as any
        if (column !== undefined) b.column = column
        Object.assign(b.style, style)
      }
    }
  }
  return clone
}

export function deepAssign(target: any, src: any) {
  for (const [k, v] of Object.entries(src || {})) {
    if (v && typeof v === 'object' && !Array.isArray(v) && target[k] && typeof target[k] === 'object') deepAssign(target[k], v)
    else target[k] = v
  }
  return target
}

const MIN_PT = 4
const WARN_PT = 6
const WARN_PX = 8 // no DPI: warn below this many output pixels

function usedStyles(markup: string): { bold: boolean; italic: boolean }[] {
  const seen = new Map<string, { bold: boolean; italic: boolean }>()
  for (const para of parseMarkup(markup))
    for (const seg of para) if (seg.text.trim()) seen.set(`${seg.bold}|${seg.italic}`, { bold: seg.bold, italic: seg.italic })
  return [...seen.values()]
}

export function computeLayout(inp: LayoutInput): LayoutResult {
  const meas = inp.measurer ?? canvasMeasurer
  const t = inp.template
  const L = t.layout
  const [sx, sy, pw, ph] = inp.sourceRect
  const warnings: Warning[] = []
  const sc = scaleFor(t.scaleMode, pw, inp.dpi)
  // files with unequal X/Y resolution: heights (and point sizes, a vertical measure) use the
  // vertical DPI, widths the horizontal one. Relative mode is unaffected (% of photo width).
  const scY = inp.dpiY === undefined ? sc : scaleFor(t.scaleMode, pw, inp.dpiY)
  // Physical template, no usable DPI: the spec says "uses relative mode and shows a warning".
  // scaleFor converts the mm/pt values to fractions of the photo width (as if printed 6 in wide);
  // see units.ts for why. There is no real DPI, so no 4 pt clamp or print-size warnings.
  if (sc.fallback)
    warnings.push({ kind: 'dpi', message: `This file has no usable print size (DPI), so sizes are set as if the photo were printed ${DEFAULT_PRINT_WIDTH_IN} in wide.` })
  const len = (v: number) => (v || 0) * sc.lenPx // horizontal lengths
  const lenY = (v: number) => (v || 0) * scY.lenPx // vertical lengths
  const R = (v: number) => Math.round(v)
  const textColors = new Set<string>()

  // --- blocks -------------------------------------------------------------
  const blocks: BlockLayout[] = []
  for (const b of t.blocks) {
    const text = inp.texts[b.id] ?? ''
    if (!plainText(text).trim()) continue
    const st = b.style
    const rf = resolveFont(st.font)
    if (rf.missing) warnings.push({ kind: 'font', block: b.id, message: `${b.name}: the font "${st.font}" isn't installed; using Source Serif 4.` })
    const fam = stack(rf.id)
    const minPx = scY.dpi ? (MIN_PT / 72) * scY.dpi : 0
    const sizePx = Math.max((st.size || 1) * scY.fontPx, minPx)
    const bl: BlockLayout = {
      block: b,
      style: st,
      sizePx,
      minPx,
      size: sizePx,
      lineH: 0,
      lines: [],
      before: lenY(st.spaceBefore),
      after: lenY(st.spaceAfter),
      fontFor: null as any,
      familyStack: fam,
      fontId: rf.id,
      ls: 0,
      wantSmallCaps: st.case === 'smallcaps',
      asc: 0,
      desc: 0,
    }
    blocks.push(bl)
    textColors.add(st.color.toLowerCase())
    // glyph coverage: characters drawn by a fallback font, and characters no font has
    const mg = missingGlyphs(rf.id, plainText(text))
    if (mg.chars.length) {
      const fbChars = mg.chars.filter((c) => !mg.unsupported.includes(c))
      if (mg.unsupported.length)
        warnings.push({ kind: 'glyph', block: b.id, message: `${b.name}: no bundled font has ${mg.unsupported.slice(0, 6).join(' ')}; those characters may not print.` })
      if (fbChars.length && mg.fallback)
        warnings.push({ kind: 'glyph', block: b.id, message: `${b.name}: ${fbChars.slice(0, 6).join(' ')} ${fbChars.length === 1 ? 'is' : 'are'} missing from ${family(rf.id)?.family}; drawn with ${mg.fallback}.` })
    }
    if (inp.gray && isColored(st.color)) warnings.push({ kind: 'color', block: b.id, message: `${b.name}: coloured text becomes grey in this greyscale file.` })
  }

  const faceOf = (bl: BlockLayout, bold: boolean, italic: boolean) => {
    const want = bold ? Math.max(700, Math.min(900, bl.style.weight + 300)) : bl.style.weight
    const aw = availableWeight(bl.fontId, want, italic || bl.style.italic)
    // real small caps only where the face that draws this text has them (no synthesis)
    return { weight: aw.weight, italic: aw.italic, smallCaps: bl.wantSmallCaps && hasSmallCaps(bl.fontId, aw.weight, aw.italic) }
  }

  // small caps: warn for every style in use whose face has no real small caps
  for (const bl of blocks) {
    if (!bl.wantSmallCaps) continue
    const styles = usedStyles(inp.texts[bl.block.id])
    const lacking = styles.filter((u) => !faceOf(bl, u.bold, u.italic).smallCaps)
    if (!lacking.length) continue
    const fname = family(bl.fontId)?.family
    const upright = !faceOf(bl, false, false).smallCaps
    if (upright && lacking.length === styles.length)
      warnings.push({ kind: 'font', block: bl.block.id, message: `${bl.block.name}: ${fname} has no small capitals; the text is shown as typed.` })
    else {
      const names = lacking.map((u) => (u.bold && u.italic ? 'bold italic' : u.bold ? 'bold' : u.italic ? 'italic' : 'regular'))
      warnings.push({ kind: 'font', block: bl.block.id, message: `${bl.block.name}: ${fname} has no small capitals in its ${names.join(', ')} style${names.length > 1 ? 's' : ''}; that text is shown as typed.` })
    }
  }

  const setScale = (s: number) => {
    for (const bl of blocks) {
      // shrink never takes text below the 4 pt minimum at the file's DPI
      const size = Math.max(bl.sizePx * s, bl.minPx)
      bl.size = size
      bl.lineH = size * (bl.style.lineHeight || 1.25)
      bl.ls = round3((size * (bl.style.letterSpacing || 0)) / 100)
      const fs = bl.familyStack
      const memo = new Map<string, FontChoice>()
      bl.fontFor = (bold: boolean, italic: boolean) => {
        const k = `${bold}|${italic}`
        let fc = memo.get(k)
        if (!fc) {
          const f = faceOf(bl, bold, italic)
          fc = { font: `${f.italic ? 'italic ' : ''}${f.weight} ${round3(size)}px ${fs}`, weight: f.weight, italic: f.italic, smallCaps: f.smallCaps }
          memo.set(k, fc)
        }
        return fc
      }
      const m = meas.metrics(bl.fontFor(false, false).font)
      bl.asc = m.ascent
      bl.desc = m.descent
    }
  }

  // --- geometry -------------------------------------------------------------
  let W: number, H: number, photoRect: Rect, bandRect: Rect, bandColor: string
  const fills: { rect: Rect; color: string }[] = []
  const protect: Rect[] = []
  const pad = L.padding
  const cols = L.columns?.count === 2 ? 2 : 1
  let fixedH: number | null
  let divH = 0
  let keyK = 0 // keyline height (top/bottom): it sits at the top of the bottom border, above the divider and text
  let keyKx = 0 // keyline width at the sides
  let spanX0: number
  let spanX1: number
  if (inp.mode === 'erase' && inp.erase) {
    ;[W, H] = inp.erase.canvas
    photoRect = inp.erase.photoRect
    bandRect = inp.erase.bandRect
    bandColor = inp.erase.bandColor
    fixedH = bandRect[3]
    const beside = bandRect[0] >= photoRect[0] + photoRect[2] || bandRect[0] + bandRect[2] <= photoRect[0]
    if (beside) {
      // a left/right band: the text uses the band's own width
      spanX0 = bandRect[0]
      spanX1 = bandRect[0] + bandRect[2]
    } else {
      // top/bottom band: the text spans the photo's width
      spanX0 = Math.max(bandRect[0], photoRect[0])
      spanX1 = Math.min(bandRect[0] + bandRect[2], photoRect[0] + photoRect[2])
    }
  } else {
    const left = R(len(L.border.left))
    const right = R(len(L.lockSides ? L.border.left : L.border.right))
    const top = R(lenY(L.border.top))
    W = left + pw + right
    photoRect = [left, top, pw, ph]
    bandColor = L.bandColor
    if (L.divider?.enabled) divH = Math.max(1, R(lenY(L.divider.width)))
    if (L.keyline?.enabled) {
      keyK = Math.max(1, R(lenY(L.keyline.width)))
      keyKx = Math.max(1, R(len(L.keyline.width)))
      const cut = [top < keyK && 'top', left < keyKx && 'left', right < keyKx && 'right'].filter(Boolean) as string[]
      if (cut.length)
        warnings.push({ kind: 'info', message: `The keyline is wider than the ${cut.join(', ')} border${cut.length > 1 ? 's' : ''}, so it is cut off there. Widen the border to show it all around.` })
    }
    fixedH = L.bandHeight.mode === 'fixed' ? R(lenY(L.border.bottom)) : null
    bandRect = [0, top + ph, W, fixedH ?? 0]
    H = 0
    // text spans the photo's width (inside side borders)
    spanX0 = Math.max(bandRect[0], photoRect[0])
    spanX1 = Math.min(bandRect[0] + bandRect[2], photoRect[0] + photoRect[2])
  }
  const topOff = keyK + divH // keyline, then divider, then padding
  const areaX0 = spanX0 + R(len(pad.left))
  const areaX1 = spanX1 - R(len(pad.right))
  let areaW = Math.max(1, areaX1 - areaX0)
  const maxW = ((spanX1 - spanX0) * (L.textMaxWidth ?? 100)) / 100
  let areaX = areaX0
  if (areaW > maxW) {
    areaX = areaX0 + (areaW - maxW) / 2
    areaW = maxW
  }
  const gutter = cols === 2 ? (areaW * (L.columns.gutter ?? 4)) / 100 : 0
  const colW = cols === 2 ? [((areaW - gutter) * L.columns.split) / 100, (areaW - gutter) * (1 - L.columns.split / 100)] : [areaW]
  const colX = cols === 2 ? [areaX, areaX + colW[0] + gutter] : [areaX]

  const layoutAll = (s: number) => {
    setScale(s)
    const heights = [0, 0]
    const perCol: BlockLayout[][] = [[], []]
    for (const bl of blocks) {
      const c = cols === 2 ? Math.min(1, Math.max(0, bl.block.column || 0)) : 0
      bl.lines = layoutBlock(bl, inp.texts[bl.block.id], Math.max(1, colW[c]), meas)
      perCol[c].push(bl)
    }
    for (let c = 0; c < cols; c++) {
      let h = 0
      perCol[c].forEach((bl, i) => {
        if (i > 0) h += bl.before * s
        h += bl.lines.length * bl.lineH
        if (i < perCol[c].length - 1) h += bl.after * s
      })
      heights[c] = h
    }
    return { heights, perCol }
  }

  const padT = lenY(pad.top)
  const padB = lenY(pad.bottom)
  let s = 1
  let res = layoutAll(1)
  let contentH = Math.max(...res.heights)
  let shrink = 1
  if (fixedH !== null) {
    const avail = fixedH - topOff - padT - padB
    if (contentH > avail + 0.5) {
      if (L.bandHeight.overflow === 'shrink' || inp.mode === 'erase') {
        let lo = 0.6
        let hi = 1
        res = layoutAll(lo)
        if (Math.max(...res.heights) <= avail) {
          for (let i = 0; i < 9; i++) {
            const mid = (lo + hi) / 2
            const r = layoutAll(mid)
            if (Math.max(...r.heights) <= avail) lo = mid
            else hi = mid
          }
          s = lo
          res = layoutAll(s)
          if (s < 0.999) warnings.push({ kind: 'info', message: `Text shrunk to ${Math.round(s * 100)}% to fit the band.` })
        } else {
          s = 0.6
          warnings.push({ kind: 'overflow', message: 'The text overflows the band even at 60% size. Shorten it or make the band taller.' })
        }
        if (blocks.some((bl) => bl.minPx && bl.sizePx * s < bl.minPx - 1e-6))
          warnings.push({ kind: 'small', message: `Shrinking stopped at the ${MIN_PT} pt minimum for some text.` })
        shrink = s
        contentH = Math.max(...res.heights)
      } else {
        warnings.push({ kind: 'overflow', message: 'The text overflows the fixed-height band. Shorten it, make the band taller, or choose Shrink to fit.' })
      }
    }
  }

  // band height for auto mode
  if (inp.mode !== 'erase') {
    const top = photoRect[1]
    let B: number
    if (fixedH !== null) B = fixedH
    else B = Math.max(R(lenY(L.bandHeight.min)), Math.ceil(topOff + padT + contentH + padB))
    if (!blocks.length && fixedH === null) B = Math.max(R(lenY(L.bandHeight.min)), topOff)
    bandRect = [0, top + ph, W, B]
    H = top + ph + B
    // fills: whole canvas border color, band color, keyline, divider (below the keyline)
    fills.push({ rect: [0, 0, W, H], color: L.borderColor })
    if (L.bandColor.toLowerCase() !== L.borderColor.toLowerCase()) fills.push({ rect: bandRect, color: L.bandColor })
    if (divH) {
      const inset = R(len(L.divider.inset))
      const r = clipRect([inset, top + ph + keyK, Math.max(0, W - 2 * inset), divH], W, H)
      fills.push({ rect: r, color: L.divider.color })
      protect.push(r)
    }
    if (keyK) {
      const k = keyK
      const kx = keyKx || keyK
      const [px, py] = photoRect
      const rs: Rect[] = [
        [px - kx, py - k, pw + 2 * kx, k],
        [px - kx, py + ph, pw + 2 * kx, k],
        [px - kx, py, kx, ph],
        [px + pw, py, kx, ph],
      ]
      for (const r of rs) {
        const c = clipRect(r, W, H)
        if (c[2] > 0 && c[3] > 0) {
          fills.push({ rect: c, color: L.keyline.color })
          protect.push(c)
        }
      }
    }
    if (inp.gray && (isColored(L.bandColor) || isColored(L.borderColor)))
      warnings.push({ kind: 'color', message: 'The band colour becomes grey in this greyscale file.' })
  }

  // --- position lines --------------------------------------------------------
  const areaTop = bandRect[1] + topOff + padT
  const availH = bandRect[3] - topOff - padT - padB
  const runs: Run[] = []
  const blockBoxes: Record<string, Rect> = {}
  const textAreas: { id: string; rect: Rect }[] = []
  for (let c = 0; c < cols; c++) {
    const colBlocks = res.perCol[c]
    const h = res.heights[c]
    let y = areaTop
    if (L.vAlign === 'middle') y += Math.max(0, (availH - h) / 2)
    else if (L.vAlign === 'bottom') y += Math.max(0, availH - h)
    textAreas.push({ id: `c${c}`, rect: [R(colX[c]), R(areaTop), Math.max(1, R(colW[c])), Math.max(1, R(availH))] })
    colBlocks.forEach((bl, i) => {
      if (i > 0) y += bl.before * s
      const top = y
      let minX = Infinity
      let maxX = -Infinity
      for (const line of bl.lines) {
        const baseline = y + (bl.lineH - (bl.asc + bl.desc)) / 2 + bl.asc
        const free = colW[c] - line.w
        let x0 = colX[c]
        const align = bl.style.align
        if (align === 'center') x0 += free / 2
        else if (align === 'right') x0 += free
        let extra = 0
        if (line.justify && free > 0) {
          const gaps = line.frags.filter((f) => f.space).length
          extra = gaps ? free / gaps : 0
        }
        let shift = 0
        for (const f of line.frags) {
          if (f.space) {
            shift += extra
            continue
          }
          const ff = bl.fontFor(f.bold, f.italic)
          runs.push({
            block: bl.block.id,
            text: f.text,
            x: round3(x0 + f.x + shift),
            y: round3(baseline),
            w: f.w,
            font: ff.font,
            family: bl.familyStack,
            weight: ff.weight,
            italic: ff.italic,
            size: round3(bl.size),
            color: bl.style.color,
            letterSpacing: bl.ls,
            smallCaps: ff.smallCaps,
            area: `c${c}`,
          })
        }
        if (line.frags.length) {
          minX = Math.min(minX, x0)
          maxX = Math.max(maxX, x0 + line.w + (extra ? free : 0))
        }
        y += bl.lineH
      }
      if (minX === Infinity) {
        minX = colX[c]
        maxX = colX[c]
      }
      blockBoxes[bl.block.id] = [minX, top, maxX - minX, y - top]
      if (i < colBlocks.length - 1) y += bl.after * s
    })
  }

  // small text warnings
  for (const bl of blocks) {
    if (scY.dpi) {
      const pt = (bl.size / scY.dpi) * 72
      if (pt < WARN_PT) warnings.push({ kind: 'small', block: bl.block.id, message: `${bl.block.name}: ${pt.toFixed(1)} pt at ${Math.round(scY.dpi)} DPI is too small to read in print.` })
    } else if (bl.size < WARN_PX) {
      warnings.push({ kind: 'small', block: bl.block.id, message: `${bl.block.name}: the text is only ${bl.size.toFixed(1)} px tall in this file and may be unreadable.` })
    }
  }
  // availability of bold/italic faces
  for (const bl of blocks) {
    const text = inp.texts[bl.block.id]
    const fid = bl.fontId
    if (/\*\*/.test(text.replace(/\\\*/g, ''))) {
      const aw = availableWeight(fid, Math.max(700, bl.style.weight + 300), bl.style.italic)
      if (aw.weight < 600) warnings.push({ kind: 'font', block: bl.block.id, message: `${bl.block.name}: ${family(fid)?.family} has no bold; shown at its regular weight.` })
    }
    if (/(^|[^*\\])\*(?!\*)/.test(text) || bl.style.italic) {
      if (!family(fid)?.faces.some((f) => f.italic)) warnings.push({ kind: 'font', block: bl.block.id, message: `${bl.block.name}: ${family(fid)?.family} has no italic; shown upright.` })
    }
  }

  for (const r of runs) textColors.add(r.color.toLowerCase())
  return {
    version: 1,
    mode: inp.mode === 'erase' ? 'erase' : 'band',
    sourceRect: [sx, sy, pw, ph],
    canvas: [W, H],
    photoRect,
    fills,
    bandColor,
    protect,
    textAreas,
    runs,
    textColors: [...textColors],
    blockBoxes,
    warnings: dedupe(warnings),
    scale: { pxPerUnit: sc.lenPx, dpi: sc.dpi, unit: t.scaleMode === 'physical' ? 'mm' : '%' },
    shrink,
  }
}

function dedupe(ws: Warning[]): Warning[] {
  const seen = new Set<string>()
  return ws.filter((w) => (seen.has(w.message) ? false : (seen.add(w.message), true)))
}

function round3(v: number) {
  return Math.round(v * 1000) / 1000
}

function clipRect(r: Rect, W: number, H: number): Rect {
  const x0 = Math.max(0, r[0])
  const y0 = Math.max(0, r[1])
  const x1 = Math.min(W, r[0] + r[2])
  const y1 = Math.min(H, r[1] + r[3])
  return [x0, y0, Math.max(0, x1 - x0), Math.max(0, y1 - y0)]
}

export function isColored(hex: string): boolean {
  const h = hex.replace('#', '')
  if (h.length < 6) return false
  const r = parseInt(h.slice(0, 2), 16)
  const g = parseInt(h.slice(2, 4), 16)
  const b = parseInt(h.slice(4, 6), 16)
  return Math.max(Math.abs(r - g), Math.abs(g - b), Math.abs(r - b)) > 6
}
