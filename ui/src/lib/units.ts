import type { ScaleMode } from './types'

export const DEFAULT_PRINT_WIDTH_IN = 6

/** Pixels per template unit. relative: 1 unit = 1% of photo width.
 * physical: lengths in mm, font sizes in pt; this returns px per mm and px per pt.
 *
 * Physical mode without a usable DPI (missing, or ≤ 72) falls back to RELATIVE mode, as the
 * spec requires. A physical template's numbers are mm/pt, not %, so "relative" needs a
 * conversion: we express them as fractions of the photo width by assuming the photo is
 * printed DEFAULT_PRINT_WIDTH_IN (6 in) wide — i.e. every size is proportional to the photo
 * width, exactly like relative mode, and the template looks the same at any resolution.
 * `dpi` is null in that case (there is no real DPI: no 4 pt clamp or print-size warnings);
 * `assumedDpi` is the conversion factor used. */
export function scaleFor(mode: ScaleMode, photoW: number, dpi: number | null) {
  const validDpi = dpi && dpi > 72.5 ? dpi : null
  if (mode === 'physical') {
    if (validDpi) return { lenPx: validDpi / 25.4, fontPx: validDpi / 72, dpi: validDpi as number | null, fallback: false, assumedDpi: validDpi }
    const d = photoW / DEFAULT_PRINT_WIDTH_IN
    return { lenPx: d / 25.4, fontPx: d / 72, dpi: null as number | null, fallback: true, assumedDpi: d }
  }
  return { lenPx: photoW / 100, fontPx: photoW / 100, dpi: validDpi as number | null, fallback: false, assumedDpi: validDpi ?? photoW / DEFAULT_PRINT_WIDTH_IN }
}

export type DisplayUnit = '%' | 'px' | 'pt' | 'mm'

/** Convert a template value (native unit) to a display unit. */
export function toDisplay(v: number, kind: 'len' | 'font', mode: ScaleMode, unit: DisplayUnit, photoW: number, dpi: number | null): number {
  const s = scaleFor(mode, photoW, dpi)
  const px = v * (kind === 'font' ? s.fontPx : s.lenPx)
  const d = s.assumedDpi
  switch (unit) {
    case '%':
      return (px / photoW) * 100
    case 'px':
      return px
    case 'pt':
      return (px / d) * 72
    case 'mm':
      return (px / d) * 25.4
  }
}

export function fromDisplay(v: number, kind: 'len' | 'font', mode: ScaleMode, unit: DisplayUnit, photoW: number, dpi: number | null): number {
  const one = toDisplay(1, kind, mode, unit, photoW, dpi)
  return one ? v / one : v
}

export function round(v: number, digits = 2): number {
  const f = Math.pow(10, digits)
  return Math.round(v * f) / f
}

/** A number typed into a number field, or NaN. The whole text must be the number (a decimal
 * comma is fine), optionally followed by the field's own unit ("12", "-1,5", "12 %", "3mm"):
 * parseFloat alone reads a prefix, so "12abc" or "1.2.3" would silently become 12 or 1.2. */
export function parseNumberInput(text: string, unit = ''): number {
  const m = /^\s*([-+]?(?:\d+(?:[.,]\d*)?|[.,]\d+))\s*(.*?)\s*$/.exec(text)
  if (!m || (m[2] && m[2].toLowerCase() !== unit.trim().toLowerCase())) return NaN
  return Number(m[1].replace(',', '.'))
}
