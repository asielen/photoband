// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { computeLayout, effectiveTemplate, type Measurer } from '../lib/layout'
import { htmlToMarkup, markupToHtml, parseMarkup, plainText } from '../lib/markup'
import type { Template } from '../lib/types'

// monospace fake: every character is 0.5em wide
const mono: Measurer = {
  width: (t, font, _sc, ls = 0) => t.length * 0.5 * parseFloat(/([\d.]+)px/.exec(font)![1]) + ls * [...t].length,
  metrics: (font) => {
    const s = parseFloat(/([\d.]+)px/.exec(font)![1])
    return { ascent: s * 0.8, descent: s * 0.2 }
  },
}

function tpl(over: Partial<Template['layout']> = {}, blocks?: Template['blocks']): Template {
  return {
    id: 't', name: 'T', scaleMode: 'relative',
    layout: {
      border: { top: 0, right: 0, bottom: 10, left: 0 }, lockSides: false, borderColor: '#ffffff', bandColor: '#ffffff', linkColors: true,
      bandHeight: { mode: 'auto', min: 5, overflow: 'warn' }, padding: { top: 1, right: 2, bottom: 1, left: 2 }, textMaxWidth: 100,
      vAlign: 'top', columns: { count: 1, split: 60, gutter: 4 }, divider: { enabled: false, width: 0.1, color: '#999999', inset: 0 },
      keyline: { enabled: false, width: 0.1, color: '#000000' }, ...over,
    } as any,
    blocks: blocks ?? [
      { id: 'a', name: 'A', format: '', column: 0, style: { font: 'x', weight: 400, italic: false, size: 2, color: '#111111', align: 'left', lineHeight: 1.25, letterSpacing: 0, spaceBefore: 0, spaceAfter: 1, case: 'none' } },
      { id: 'b', name: 'B', format: '', column: 1, style: { font: 'x', weight: 400, italic: false, size: 2, color: '#333333', align: 'left', lineHeight: 1.25, letterSpacing: 0, spaceBefore: 0, spaceAfter: 0, case: 'none' } },
    ],
  }
}

const base = { sourceRect: [0, 0, 1000, 800] as [number, number, number, number], dpi: null, mode: 'band' as const, measurer: mono }

describe('layout engine', () => {
  it('wraps greedily at spaces and is deterministic', () => {
    // font 20px → 10px per char; area = 1000 - 40 = 960px → 96 chars per line
    const text = 'word '.repeat(40).trim()
    const a = computeLayout({ ...base, template: tpl(), texts: { a: text, b: '' } })
    const b = computeLayout({ ...base, template: tpl(), texts: { a: text, b: '' } })
    expect(JSON.stringify(a)).toEqual(JSON.stringify(b))
    const lines = new Set(a.runs.map((r) => r.y))
    expect(lines.size).toBe(3)
    for (const r of a.runs) expect(r.x + r.w).toBeLessThanOrEqual(20 + 960 + 0.01)
  })

  it('empty blocks take no space and auto band grows with text', () => {
    const one = computeLayout({ ...base, template: tpl(), texts: { a: 'Hi', b: '' } })
    const two = computeLayout({ ...base, template: tpl(), texts: { a: 'Hi', b: 'There' } })
    expect(two.canvas[1]).toBeGreaterThan(one.canvas[1])
    expect(one.runs.every((r) => r.block === 'a')).toBe(true)
    // min band height is respected
    const none = computeLayout({ ...base, template: tpl(), texts: { a: '', b: '' } })
    expect(none.canvas[1]).toBe(800 + 50)
  })

  it('photo placement and fills', () => {
    const l = computeLayout({ ...base, template: tpl({ border: { top: 5, right: 4, bottom: 10, left: 4 } }), texts: { a: 'x', b: '' } })
    expect(l.photoRect).toEqual([40, 50, 1000, 800])
    expect(l.canvas[0]).toBe(1080)
    expect(l.fills[0].rect).toEqual([0, 0, 1080, l.canvas[1]])
  })

  it('center and right alignment', () => {
    const t = tpl()
    t.blocks[0].style.align = 'center'
    const l = computeLayout({ ...base, template: t, texts: { a: 'abcd', b: '' } })
    const r = l.runs[0]
    expect(Math.abs(r.x + r.w / 2 - 500)).toBeLessThan(0.01)
    t.blocks[0].style.align = 'right'
    const l2 = computeLayout({ ...base, template: t, texts: { a: 'abcd', b: '' } })
    expect(Math.abs(l2.runs[0].x + l2.runs[0].w - 980)).toBeLessThan(0.01)
  })

  it('justify spreads all but the last line', () => {
    const t = tpl()
    t.blocks[0].style.align = 'justify'
    const l = computeLayout({ ...base, template: t, texts: { a: 'aaaa '.repeat(30).trim(), b: '' } })
    const firstLineY = l.runs[0].y
    const first = l.runs.filter((r) => r.y === firstLineY)
    const last = first[first.length - 1]
    expect(Math.abs(last.x + last.w - 980)).toBeLessThan(0.5)
  })

  it('breaks an over-long word by character and honours hyphens', () => {
    const l = computeLayout({ ...base, template: tpl(), texts: { a: 'x'.repeat(200), b: '' } })
    expect(new Set(l.runs.map((r) => r.y)).size).toBe(3)
    const h = computeLayout({ ...base, template: tpl(), texts: { a: 'a'.repeat(60) + '-' + 'b'.repeat(60), b: '' } })
    expect(h.runs[0].text.endsWith('-')).toBe(true)
  })

  it('fixed band shrinks to fit with a 60% floor and warns beyond', () => {
    const t = tpl({ bandHeight: { mode: 'fixed', min: 5, overflow: 'shrink' } as any, border: { top: 0, right: 0, bottom: 8, left: 0 } })
    const fits = computeLayout({ ...base, template: t, texts: { a: 'short', b: '' } })
    expect(fits.shrink).toBe(1)
    const long = computeLayout({ ...base, template: t, texts: { a: 'line\n'.repeat(3) + 'end', b: '' } })
    expect(long.shrink).toBeLessThan(1)
    expect(long.shrink).toBeGreaterThanOrEqual(0.6)
    const huge = computeLayout({ ...base, template: t, texts: { a: 'line\n'.repeat(20), b: '' } })
    expect(huge.warnings.some((w) => w.kind === 'overflow')).toBe(true)
    expect(huge.canvas[1]).toBe(880)
  })

  it('two columns place blocks side by side', () => {
    const t = tpl({ columns: { count: 2, split: 60, gutter: 4 } as any })
    const l = computeLayout({ ...base, template: t, texts: { a: 'left', b: 'right' } })
    const ra = l.runs.find((r) => r.block === 'a')!
    const rb = l.runs.find((r) => r.block === 'b')!
    expect(rb.x).toBeGreaterThan(ra.x + 400)
    expect(Math.abs(ra.y - rb.y)).toBeLessThan(0.01)
    expect(l.textAreas.length).toBe(2)
  })

  it('bold and italic runs split and merge', () => {
    const l = computeLayout({ ...base, template: tpl(), texts: { a: 'plain **bold** tail', b: '' } })
    expect(l.runs.map((r) => r.text)).toEqual(['plain', 'bold', 'tail'])
    // spaces are kept as positions: the next run starts one space after the previous word
    expect(l.runs[1].x - (l.runs[0].x + 5 * 10)).toBeCloseTo(10)
    expect(l.runs[1].weight).toBeGreaterThanOrEqual(700)
  })

  it('divider and keyline are protected from the marker', () => {
    const t = tpl({ divider: { enabled: true, width: 0.2, color: '#999999', inset: 1 } as any, keyline: { enabled: true, width: 0.2, color: '#000000' } as any, border: { top: 2, right: 2, bottom: 10, left: 2 } })
    const l = computeLayout({ ...base, template: t, texts: { a: 'x', b: '' } })
    expect(l.protect.length).toBe(5)
    expect(l.fills.length).toBe(6)
  })

  it('erase mode keeps the source canvas', () => {
    const l = computeLayout({
      ...base, mode: 'erase', template: tpl(), texts: { a: 'New', b: '' },
      erase: { canvas: [1100, 1000], photoRect: [50, 50, 1000, 800], bandRect: [0, 850, 1100, 150], bandColor: '#f4f1ea' },
    })
    expect(l.canvas).toEqual([1100, 1000])
    expect(l.fills.length).toBe(0)
    expect(l.runs[0].y).toBeGreaterThan(850)
  })

  it('overrides merge onto the template', () => {
    const t = tpl()
    const e = effectiveTemplate(t, { layout: { border: { bottom: 20 } }, blocks: { a: { size: 3, column: 1 } as any } })
    expect(e.layout.border.bottom).toBe(20)
    expect(e.layout.border.top).toBe(0)
    expect(e.blocks[0].style.size).toBe(3)
    expect(e.blocks[0].column).toBe(1)
    expect(t.blocks[0].style.size).toBe(2)
  })
})

describe('layout engine: review fixes', () => {
  it('a break at a soft hyphen fits the hyphen too', () => {
    // 10 px per char, line 960 px: "a"*90 + " " + "bbbbb" = 960 exactly; the hyphen would make 970
    const text = 'a'.repeat(90) + ' bbbbb\u00adccccccccccc'
    const l = computeLayout({ ...base, template: tpl(), texts: { a: text, b: '' } })
    for (const r of l.runs) expect(r.x + r.w).toBeLessThanOrEqual(980 + 0.01)
    const ys = [...new Set(l.runs.map((r) => r.y))]
    expect(ys.length).toBe(2)
    expect(l.runs.filter((r) => r.y === ys[0]).map((r) => r.text)).toEqual(['a'.repeat(90)])
    expect(l.runs.filter((r) => r.y === ys[1]).map((r) => r.text).join('')).toBe('bbbbbccccccccccc')
    // and when the hyphenated piece does fit, the hyphen is shown
    const t2 = 'a'.repeat(80) + ' bbbbb\u00adccccccccccccccc'
    const l2 = computeLayout({ ...base, template: tpl(), texts: { a: t2, b: '' } })
    expect(l2.runs[0].text).toBe('a'.repeat(80) + ' bbbbb-')
    expect(l2.runs[0].x + l2.runs[0].w).toBeLessThanOrEqual(980 + 0.01)
  })

  it('letter spacing is part of every measured width', () => {
    const t = tpl()
    t.blocks[0].style.letterSpacing = 10 // 10% of 20 px = 2 px after every character
    const l = computeLayout({ ...base, template: t, texts: { a: 'ab cd', b: '' } })
    expect(l.runs[0].text).toBe('ab cd')
    expect(l.runs[0].letterSpacing).toBe(2)
    expect(l.runs[0].w).toBeCloseTo(5 * 10 + 5 * 2)
  })

  it('shrink-to-fit never goes below 4 pt at the file DPI', () => {
    const t = tpl({ bandHeight: { mode: 'fixed', min: 5, overflow: 'shrink' } as any, border: { top: 0, right: 0, bottom: 8, left: 0 } })
    // 20 px at s=1; 4 pt at 300 dpi = 16.67 px, above the 60% floor (12 px)
    const l = computeLayout({ ...base, dpi: 300, template: t, texts: { a: 'line\n'.repeat(20), b: '' } })
    const minPx = (4 / 72) * 300
    expect(l.runs.length).toBeGreaterThan(0)
    for (const r of l.runs) expect(r.size).toBeGreaterThanOrEqual(minPx - 0.01)
    expect(l.warnings.some((w) => w.kind === 'overflow')).toBe(true)
    // a moderate overflow shrinks, but only down to the clamp
    const m = computeLayout({ ...base, dpi: 300, template: t, texts: { a: 'line\n'.repeat(2) + 'end', b: '' } })
    for (const r of m.runs) expect(r.size).toBeGreaterThanOrEqual(minPx - 0.01)
  })

  it('erase mode with a side band uses the band width', () => {
    const l = computeLayout({
      ...base, mode: 'erase', template: tpl(), texts: { a: 'Side text', b: '' },
      erase: { canvas: [1300, 800], photoRect: [0, 0, 1000, 800], bandRect: [1000, 0, 300, 800], bandColor: '#ffffff' },
    })
    // padding 2% of the 1000 px photo on each side
    expect(l.textAreas[0].rect[0]).toBe(1020)
    expect(l.textAreas[0].rect[2]).toBe(260)
    expect(l.runs[0].x).toBeGreaterThanOrEqual(1020)
    expect(l.runs[0].text).toBe('Side text')
    // top/bottom bands still span the photo only
    const b = computeLayout({
      ...base, mode: 'erase', template: tpl(), texts: { a: 'x', b: '' },
      erase: { canvas: [1100, 1000], photoRect: [50, 50, 1000, 800], bandRect: [0, 850, 1100, 150], bandColor: '#ffffff' },
    })
    expect(b.textAreas[0].rect[0]).toBe(70)
    expect(b.textAreas[0].rect[2]).toBe(960)
  })

  it('the keyline sits above the divider and the text, and warns when borders are too thin', () => {
    const t = tpl({ keyline: { enabled: true, width: 0.5, color: '#000000' } as any, divider: { enabled: true, width: 0.2, color: '#999999', inset: 0 } as any })
    const l = computeLayout({ ...base, template: t, texts: { a: 'x', b: '' } })
    // keyline 5 px, divider 2 px, padding top 10 px
    const key = l.fills.find((f) => f.color === '#000000' && f.rect[1] === 800)!
    expect(key.rect[3]).toBe(5)
    const div = l.fills.find((f) => f.color === '#999999')!
    expect(div.rect[1]).toBe(805)
    expect(l.textAreas[0].rect[1]).toBe(817)
    expect(l.runs[0].y - 20 * 0.8).toBeGreaterThanOrEqual(817 - 0.01)
    // borders of 0 on top/left/right: the keyline only shows at the bottom
    expect(l.warnings.some((w) => /keyline/i.test(w.message))).toBe(true)
    const ok = computeLayout({ ...base, template: tpl({ keyline: { enabled: true, width: 0.5, color: '#000000' } as any, border: { top: 1, right: 1, bottom: 10, left: 1 } }), texts: { a: 'x', b: '' } })
    expect(ok.warnings.some((w) => /keyline/i.test(w.message))).toBe(false)
    // fixed band: the keyline takes its share of the fixed bottom border (canvas height unchanged)
    const fx = computeLayout({ ...base, template: tpl({ bandHeight: { mode: 'fixed', min: 5, overflow: 'warn' } as any, keyline: { enabled: true, width: 0.5, color: '#000000' } as any }), texts: { a: 'x', b: '' } })
    expect(fx.canvas[1]).toBe(900)
    expect(fx.textAreas[0].rect[1]).toBe(815)
  })

  it('physical mode without a usable DPI falls back to relative sizing and says so', () => {
    const t = tpl()
    t.scaleMode = 'physical'
    t.blocks[0].style.size = 10 // pt
    const src = { ...base, sourceRect: [0, 0, 1800, 1200] as [number, number, number, number] }
    for (const dpi of [null, 72]) {
      const l = computeLayout({ ...src, dpi, template: t, texts: { a: 'x', b: '' } })
      // as if printed 6 in wide: 1800 px / 6 in = 300 px per inch → 10 pt = 41.67 px
      expect(l.runs[0].size).toBeCloseTo((10 / 72) * 300, 2)
      expect(l.warnings.find((w) => w.kind === 'dpi')?.message).toBe('This file has no usable print size (DPI), so sizes are set as if the photo were printed 6 in wide.')
      expect(l.scale.dpi).toBe(null)
    }
    // the same photo at twice the pixels keeps the same proportions (relative behaviour)
    const big = computeLayout({ ...src, sourceRect: [0, 0, 3600, 2400], dpi: null, template: t, texts: { a: 'x', b: '' } })
    expect(big.runs[0].size).toBeCloseTo((10 / 72) * 600, 2)
    const real = computeLayout({ ...src, dpi: 600, template: t, texts: { a: 'x', b: '' } })
    expect(real.runs[0].size).toBeCloseTo((10 / 72) * 600, 2)
    expect(real.warnings.some((w) => w.kind === 'dpi')).toBe(false)
  })

  it('warns about tiny text when there is no DPI', () => {
    const t = tpl()
    t.blocks[0].style.size = 0.5 // 0.5% of 1000 px = 5 px
    const l = computeLayout({ ...base, template: t, texts: { a: 'tiny', b: '' } })
    expect(l.warnings.some((w) => w.kind === 'small')).toBe(true)
  })

  it('merged runs keep lines within the width', () => {
    const t = tpl()
    const l = computeLayout({ ...base, template: t, texts: { a: ('word **bold** *it* ').repeat(30), b: '' } })
    for (const r of l.runs) expect(r.x + r.w).toBeLessThanOrEqual(980 + 0.01)
    // runs on a line never overlap
    const byLine = new Map<number, typeof l.runs>()
    for (const r of l.runs) byLine.set(r.y, [...(byLine.get(r.y) || []), r])
    for (const rs of byLine.values()) for (let i = 1; i < rs.length; i++) expect(rs[i].x).toBeGreaterThanOrEqual(rs[i - 1].x + rs[i - 1].w - 0.01)
  })
})

describe('markup', () => {
  it('parses styles and escapes', () => {
    expect(parseMarkup('a **b** *c* \\*d')).toEqual([[
      { text: 'a ', bold: false, italic: false },
      { text: 'b', bold: true, italic: false },
      { text: ' ', bold: false, italic: false },
      { text: 'c', bold: false, italic: true },
      { text: ' *d', bold: false, italic: false },
    ]])
    expect(plainText('x\\\\y\n**z**')).toBe('x\\y\nz')
    expect(markupToHtml('**a** <b>')).toBe('<b>a</b> &lt;b&gt;')
  })
})
