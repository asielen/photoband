// Drawing a layout: the same runs, at any scale. The preview draws them scaled onto
// the pane; saving draws them at output scale into transparent tiles.
//
// Runs are always drawn at their OUTPUT font size under a canvas transform, never by
// scaling the font size: a font drawn at a small size gets hinted/rounded advances and
// comes out wider than the scaled-down output. Drawing at output size under a scale
// transform keeps the preview's text the same shape as the saved file's.

import type { LayoutResult, Rect, Run } from './types'

export const hasLetterSpacing =
  typeof CanvasRenderingContext2D !== 'undefined' && 'letterSpacing' in CanvasRenderingContext2D.prototype

/** Text settings shared by the measurer (layout.ts) and the drawer, so measure == draw. */
export function applyTextSettings(ctx: CanvasRenderingContext2D, font: string, smallCaps: boolean, ls: number) {
  ctx.font = font
  const c = ctx as any
  c.fontKerning = 'normal'
  c.fontVariantCaps = smallCaps ? 'small-caps' : 'normal'
  if ('textRendering' in c) c.textRendering = 'geometricPrecision'
  // Chrome disables ligatures while letterSpacing ≠ 0, in measureText and fillText alike
  if (hasLetterSpacing) c.letterSpacing = `${ls || 0}px`
}

let _seg: any = null
/** User-perceived characters (grapheme clusters); code points where Intl.Segmenter is missing. */
export function graphemes(s: string): string[] {
  if (_seg === null) {
    const I = (globalThis as any).Intl
    _seg = I && I.Segmenter ? new I.Segmenter(undefined, { granularity: 'grapheme' }) : false
  }
  if (_seg) return Array.from(_seg.segment(s), (x: any) => x.segment as string)
  return Array.from(s)
}

/** Width of `text` as drawRuns draws it, with the ctx already set up by applyTextSettings.
 * Native letterSpacing: measureText includes it. Manual fallback: glyph i is drawn at
 * x + measure(prefix_i) + i·ls, and the run advances by measure(text) + n·ls (CSS semantics:
 * spacing after every character, as the native path does). */
export function measureDrawn(ctx: CanvasRenderingContext2D, text: string, ls: number): number {
  const w = ctx.measureText(text).width
  if (hasLetterSpacing || !ls) return w
  return w + ls * graphemes(text).length
}

export function fontAt(run: Run, scale = 1): string {
  return `${run.italic ? 'italic ' : ''}${run.weight} ${Math.max(0.01, run.size * scale)}px ${run.family}`
}

/** Draw runs at output size under a transform: 1 output px = `scale` ctx units, output (0,0) at (ox, oy). */
export function drawRuns(ctx: CanvasRenderingContext2D, runs: Run[], scale: number, ox: number, oy: number) {
  ctx.save()
  ctx.translate(ox, oy)
  if (scale !== 1) ctx.scale(scale, scale)
  ctx.textBaseline = 'alphabetic'
  ctx.textAlign = 'left'
  for (const r of runs) {
    applyTextSettings(ctx, fontAt(r), r.smallCaps, r.letterSpacing)
    ctx.fillStyle = r.color
    const ls = r.letterSpacing
    if (!ls || hasLetterSpacing) {
      ctx.fillText(r.text, r.x, r.y)
    } else {
      // manual tracking (no ctx.letterSpacing, older WebKit): same formula the measurer uses
      let prefix = ''
      graphemes(r.text).forEach((g, i) => {
        const px = prefix ? ctx.measureText(prefix).width : 0
        ctx.fillText(g, r.x + px + i * ls, r.y)
        prefix += g
      })
    }
  }
  ctx.restore()
}

export function drawFills(ctx: CanvasRenderingContext2D, lay: LayoutResult, scale: number, ox: number, oy: number) {
  for (const f of lay.fills) {
    ctx.fillStyle = f.color
    const [x, y, w, h] = f.rect
    ctx.fillRect(ox + x * scale, oy + y * scale, w * scale, h * scale)
  }
}

export interface Tile {
  x: number
  y: number
  blob: Blob
}

export const TILE_MAX = 4096 // under WebView canvas limits (WKWebView is the strictest)

let _bctx: CanvasRenderingContext2D | null = null
/** Ink bounds of a run in output px: glyph bounding box (covers italic overhang and negative
 * spacing), unioned with the advance box, plus a margin of 0.15em for antialiasing/overshoot. */
export function runInkBounds(r: Run): [number, number, number, number] {
  if (!_bctx) _bctx = document.createElement('canvas').getContext('2d')!
  applyTextSettings(_bctx, fontAt(r), r.smallCaps, r.letterSpacing)
  const tm = _bctx.measureText(r.text)
  const m = Math.max(2, r.size * 0.15)
  const left = Math.min(r.x, r.x - (tm.actualBoundingBoxLeft || 0))
  const right = Math.max(r.x + r.w, r.x + (tm.actualBoundingBoxRight || 0), r.x + measureDrawn(_bctx, r.text, r.letterSpacing))
  const asc = Math.max(tm.actualBoundingBoxAscent || 0, tm.fontBoundingBoxAscent || r.size)
  const desc = Math.max(tm.actualBoundingBoxDescent || 0, tm.fontBoundingBoxDescent || r.size * 0.3)
  return [Math.floor(left - m), Math.floor(r.y - asc - m), Math.ceil(right + m), Math.ceil(r.y + desc + m)]
}

/** Render the text layer at output scale into ≤4096 px tiles covering each area's ink. */
export async function renderTextTiles(lay: LayoutResult): Promise<Tile[]> {
  const tiles: Tile[] = []
  const [W, H] = lay.canvas
  for (const area of lay.textAreas) {
    const runs = lay.runs.filter((r) => r.area === area.id)
    if (!runs.length) continue
    const bounds = runs.map(runInkBounds)
    let x0 = Infinity
    let y0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    for (const b of bounds) {
      x0 = Math.min(x0, b[0])
      y0 = Math.min(y0, b[1])
      x1 = Math.max(x1, b[2])
      y1 = Math.max(y1, b[3])
    }
    // clamp to the canvas only; ink may extend past the text area (overhangs, descenders)
    x0 = Math.max(0, x0)
    y0 = Math.max(0, y0)
    x1 = Math.min(W, x1)
    y1 = Math.min(H, y1)
    for (let ty = y0; ty < y1; ty += TILE_MAX) {
      for (let tx = x0; tx < x1; tx += TILE_MAX) {
        const tw = Math.min(TILE_MAX, x1 - tx)
        const th = Math.min(TILE_MAX, y1 - ty)
        const tileRuns = runs.filter((_, i) => {
          const b = bounds[i]
          return b[0] < tx + tw && b[2] > tx && b[1] < ty + th && b[3] > ty
        })
        if (!tileRuns.length) continue
        const cv = document.createElement('canvas')
        cv.width = tw
        cv.height = th
        const ctx = cv.getContext('2d')!
        ctx.clearRect(0, 0, tw, th)
        drawRuns(ctx, tileRuns, 1, -tx, -ty)
        const blob: Blob = await new Promise((res, rej) => cv.toBlob((b) => (b ? res(b) : rej(new Error('Could not render the text layer'))), 'image/png'))
        tiles.push({ x: tx, y: ty, blob })
        cv.width = cv.height = 0
      }
    }
  }
  return tiles
}

export function rectContains(r: Rect, x: number, y: number) {
  return x >= r[0] && y >= r[1] && x <= r[0] + r[2] && y <= r[1] + r[3]
}
