// Font registry: every font the layout uses is loaded from the backend as a
// FontFace under an internal family name, so preview and output use the same file.
import { get, url } from './api'
import type { FontFamily } from './types'

export let families: FontFamily[] = []
let byId = new Map<string, FontFamily>()
export let fallbackIds: string[] = ['noto-serif', 'noto-sans']
export let defaultFamily = 'source-serif-4'
const loaded = new Map<string, Promise<void>>()

// --- change notification ---------------------------------------------------------------
// Fired when a font family finishes loading new faces or the registry is reloaded, so
// measure caches can be cleared and the layout recomputed with the real font metrics.
type FontsListener = () => void
const listeners = new Set<FontsListener>()
export function onFontsChanged(cb: FontsListener): () => void {
  listeners.add(cb)
  return () => listeners.delete(cb)
}
function fontsChanged() {
  for (const cb of [...listeners]) {
    try {
      cb()
    } catch (e) {
      console.error(e)
    }
  }
}

export async function loadRegistry(refresh = false) {
  const r = await get<{ families: FontFamily[]; fallback: string[]; default: string }>('/api/fonts', { refresh: refresh ? 1 : 0 })
  families = r.families
  byId = new Map(families.map((f) => [f.id, f]))
  fallbackIds = r.fallback
  defaultFamily = r.default
  fontsChanged()
  return families
}

export function family(id: string): FontFamily | undefined {
  return byId.get(id)
}

export function cssName(id: string) {
  return `pbf-${id}`
}

function isSans(f: FontFamily | undefined) {
  if (!f) return false
  const r = (f.role + ' ' + f.family).toLowerCase()
  return r.includes('sans') || r.includes('inter') || r.includes('grotesk')
}

/** Family stack for canvas: the chosen font, then Noto fallbacks (sans first for sans fonts). */
export function stack(id: string): string {
  const f = byId.get(id)
  const fb = isSans(f) ? ['noto-sans', 'noto-serif'] : ['noto-serif', 'noto-sans']
  return [id, ...fb.filter((x) => x !== id)].map((x) => `"${cssName(x)}"`).join(', ')
}

/** Resolve a template font id: missing fonts fall back to the default family. */
export function resolveFont(id: string): { id: string; missing: boolean } {
  if (byId.has(id)) return { id, missing: false }
  return { id: defaultFamily, missing: true }
}

export async function ensureFamily(id: string): Promise<void> {
  const f = byId.get(id)
  if (!f) return
  // keyed by the family's files, so a rescan that adds faces loads them
  const key = id + ':' + f.faces.map((fc) => fc.file).join(',')
  let p = loaded.get(key)
  if (!p) {
    p = (async () => {
      const faces = f.faces.map((fc) => {
        const weight = fc.weightRange ? `${fc.weightRange[0]} ${fc.weightRange[1]}` : String(fc.weight)
        const ff = new FontFace(cssName(id), `url(${url('/fonts/file/' + fc.file)})`, {
          weight,
          style: fc.italic ? 'italic' : 'normal',
          display: 'block',
        })
        ;(document as any).fonts.add(ff)
        return ff.load().then(
          () => true,
          (e: any) => {
            console.warn('font failed', f.family, e)
            try {
              ;(document as any).fonts.delete(ff)
            } catch {
              /* ignore */
            }
            return false
          },
        )
      })
      const ok = await Promise.all(faces)
      // a failed face must not stick: evict so the next ensureFamily() retries
      if (ok.some((x) => !x) && loaded.get(key) === p) loaded.delete(key)
      if (ok.some((x) => x)) fontsChanged()
    })()
    loaded.set(key, p)
  }
  return p
}

export async function ensureFonts(ids: string[]) {
  const all = new Set([...ids, ...fallbackIds])
  await Promise.all([...all].map((id) => ensureFamily(id)))
}

/** Nearest weight the family really has (no synthetic bold). */
export function availableWeight(id: string, want: number, italic: boolean): { weight: number; italic: boolean; exact: boolean } {
  const f = byId.get(id)
  if (!f || !f.faces.length) return { weight: want, italic, exact: true }
  let faces = f.faces.filter((fc) => fc.italic === italic)
  let it = italic
  if (!faces.length) {
    faces = f.faces
    it = !italic ? false : faces.some((x) => x.italic)
  }
  let best = want
  let bestD = Infinity
  for (const fc of faces) {
    const [lo, hi] = fc.weightRange ?? [fc.weight, fc.weight]
    const w = Math.min(hi, Math.max(lo, want))
    const d = Math.abs(w - want)
    if (d < bestD) {
      bestD = d
      best = w
    }
  }
  return { weight: best, italic: it, exact: bestD === 0 && it === italic }
}

type FaceWithCaps = FontFamily['faces'][number] & { smallCaps?: boolean }

/** The face that serves `weight` (after availableWeight) in the given style. */
export function faceFor(id: string, weight: number, italic: boolean): FaceWithCaps | undefined {
  const f = byId.get(id)
  if (!f || !f.faces.length) return undefined
  let faces = f.faces.filter((fc) => fc.italic === italic)
  if (!faces.length) faces = f.faces
  let best: FaceWithCaps | undefined
  let bestD = Infinity
  for (const fc of faces) {
    const [lo, hi] = fc.weightRange ?? [fc.weight, fc.weight]
    const d = weight < lo ? lo - weight : weight > hi ? weight - hi : 0
    if (d < bestD) {
      bestD = d
      best = fc
    }
  }
  return best
}

/** Real small caps (GSUB smcp) in the face that draws this weight/style. Older registries
 * without per-face data fall back to the family flag. */
export function hasSmallCaps(id: string, weight: number, italic: boolean): boolean {
  const f = byId.get(id)
  if (!f) return false
  const v = faceFor(id, weight, italic)?.smallCaps
  return typeof v === 'boolean' ? v : !!f.smallCaps
}

export function weightsFor(id: string): number[] {
  const f = byId.get(id)
  if (!f) return [400]
  const set = new Set<number>()
  for (const fc of f.faces) {
    if (fc.weightRange) {
      for (let w = 100; w <= 900; w += 100) if (w >= fc.weightRange[0] && w <= fc.weightRange[1]) set.add(w)
    } else set.add(fc.weight)
  }
  return [...set].sort((a, b) => a - b)
}

export function hasItalic(id: string): boolean {
  return !!byId.get(id)?.faces.some((f) => f.italic)
}

function covers(ranges: [number, number][], cp: number): boolean {
  let lo = 0
  let hi = ranges.length - 1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    const [a, b] = ranges[mid]
    if (cp < a) hi = mid - 1
    else if (cp > b) lo = mid + 1
    else return true
  }
  return false
}

/** Characters of `text` the family lacks, and which fallback draws them. */
export function missingGlyphs(id: string, text: string): { chars: string[]; fallback: string | null; unsupported: string[] } {
  const f = byId.get(id)
  if (!f) return { chars: [], fallback: null, unsupported: [] }
  const missing = new Set<string>()
  for (const ch of text) {
    const cp = ch.codePointAt(0)!
    if (cp < 0x20 || /\s/.test(ch) || (cp >= 0x300 && cp <= 0x36f) || cp === 0xad || cp === 0x200b) continue
    if (!covers(f.coverage, cp)) missing.add(ch)
  }
  if (!missing.size) return { chars: [], fallback: null, unsupported: [] }
  const fbOrder = isSans(f) ? ['noto-sans', 'noto-serif'] : ['noto-serif', 'noto-sans']
  let fallback: string | null = null
  const unsupported: string[] = []
  for (const ch of missing) {
    const cp = ch.codePointAt(0)!
    const fb = fbOrder.find((x) => byId.get(x) && covers(byId.get(x)!.coverage, cp))
    if (fb) fallback = fallback ?? byId.get(fb)!.family
    else unsupported.push(ch)
  }
  return { chars: [...missing], fallback, unsupported }
}
