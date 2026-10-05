// Edited photo details (the draft's `meta`): the shapes the backend's metaedit.py takes, and the
// small pure helpers the editor needs right away (faces on screen, the date editor's level, the
// keyword chips, name suggestions). The backend applies the same edits for captions and saving.

import type { Face, PhotoMeta } from './types'
import { loadPref, savePref } from './prefs'

export type Box = [number, number, number, number]
export type DateLevel = 'day' | 'month' | 'year' | 'circa'

export interface FaceEdit {
  name?: string
  box?: Box | null
  deleted?: boolean
  /** the face as the file had it when the edit started (to find it again in a changed file) */
  was?: { name: string; box: Box | null }
}

export interface DateEdit {
  iso: string // '1952' | '1952-06' | '1952-06-14'
  level: DateLevel
}

export interface MetaEdits {
  title?: string
  caption?: string
  notes?: string
  creator?: string
  sublocation?: string
  city?: string
  state?: string
  country?: string
  keywords?: string[]
  date?: DateEdit | null
  faces?: Record<string, FaceEdit>
}

export const TEXT_FIELDS = ['title', 'caption', 'notes', 'creator', 'sublocation', 'city', 'state', 'country'] as const
export type TextField = (typeof TEXT_FIELDS)[number]

export const LEVEL_PATTERN: Record<DateLevel, string> = { day: 'Y!M!D!', month: 'Y!M!', year: 'Y!', circa: 'Y~' }

/** Number of edited details (each edited face counts once). */
export function editCount(m: MetaEdits | null | undefined): number {
  if (!m) return 0
  let n = 0
  for (const k of Object.keys(m) as (keyof MetaEdits)[]) {
    if (k === 'faces') n += Object.keys(m.faces || {}).length
    else n++
  }
  return n
}

export function hasEdits(m: MetaEdits | null | undefined): boolean {
  return editCount(m) > 0
}

/** The faces as they will be once the edits are written: named ones, and those without a name. */
export function effectiveFaces(meta: PhotoMeta | null | undefined, m: MetaEdits | null | undefined): { named: Face[]; unnamed: Face[] } {
  const base = [...(meta?.faces?.named ?? []), ...(meta?.faces?.unnamed ?? [])]
  const edits = m?.faces
  if (!edits || !Object.keys(edits).length) return { named: meta?.faces?.named ?? [], unnamed: meta?.faces?.unnamed ?? [] }
  const out: Face[] = []
  for (const f of base) {
    const e = f.key ? edits[f.key] : undefined
    if (!e) {
      out.push(f)
      continue
    }
    if (e.deleted) continue
    out.push({ ...f, name: e.name !== undefined ? e.name : f.name, box: e.box !== undefined ? e.box : f.box })
  }
  for (const [k, e] of Object.entries(edits)) {
    if (k.startsWith('new:') && !e.deleted) out.push({ name: e.name ?? '', box: e.box ?? null, source: 'edit', ids: [], key: k })
  }
  return { named: out.filter((f) => f.name.trim()), unnamed: out.filter((f) => !f.name.trim()) }
}

/** Left-to-right order of positioned faces (the caption's order). */
export function leftToRight(faces: Face[]): Face[] {
  return faces.filter((f) => f.box).sort((a, b) => a.box![0] + a.box![2] / 2 - (b.box![0] + b.box![2] / 2))
}

export function newFaceKey(): string {
  return 'new:' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4)
}

// ---------------------------------------------------------------------------- dates

export type DateState = { kind: 'none' } | { kind: 'date'; iso: string; level: DateLevel } | { kind: 'text'; text: string; year: number | null }

const DATE_RE = /^\s*(\d{4})(?:[-:/.](\d{1,2})(?:[-:/.](\d{1,2}))?)?(?=$|[\sT])(.*)$/

function pad(n: number) {
  return String(n).padStart(2, '0')
}

/** What the date editor shows for the file's date: a date with its level of detail (from photokin's
 *  "DATE:" certainty when it applies, else from how much of the date is written), a date written as
 *  text ("1950s"), or none. */
export function dateState(fields: Record<string, any> | null | undefined): DateState {
  const raw = String(fields?.date ?? '').trim()
  if (!raw || !/[1-9]/.test(raw)) return { kind: 'none' }
  const m = DATE_RE.exec(raw)
  const rest = m?.[4]?.trim() ?? ''
  const tail = !rest || /^T?\d{1,2}:\d{2}/.test(rest) || /^[\s:]*$/.test(rest)
  if (!m || !tail) {
    const y = /(?<!\d)(1\d{3}|20\d{2})(?!\d)/.exec(raw)
    return { kind: 'text', text: raw, year: y ? +y[1] : null }
  }
  const y = +m[1]
  const mo = m[2] ? +m[2] : 0
  const d = mo && m[3] ? +m[3] : 0
  if (mo > 12 || d > 31) return { kind: 'text', text: raw, year: y }
  const pattern = String(fields?.date_certainty ?? '').toUpperCase()
  let level: DateLevel = d ? 'day' : mo ? 'month' : 'year'
  const pm = /^Y(.)(?:M(.))?(?:D(.))?$/.exec(pattern)
  if (pm) {
    const [, py, pmo, pd] = pm
    if (py === '?') return { kind: 'none' }
    if (py !== '!') level = 'circa'
    else if (pmo === '!' && pd === '!' && d) level = 'day'
    else if (pmo === '!' && mo) level = 'month'
    else level = 'year'
  }
  const iso = level === 'day' ? `${y}-${pad(mo)}-${pad(d)}` : level === 'month' ? `${y}-${pad(mo || 6)}` : `${y}`
  return { kind: 'date', iso, level }
}

/** An ISO date for a level from year / month / day inputs, or null when they don't make one. */
export function isoFor(level: DateLevel, y: number | null, mo: number | null, d: number | null): string | null {
  if (!y || y < 1000 || y > new Date().getFullYear() + 1) return null
  if (level === 'year' || level === 'circa') return String(y)
  if (!mo || mo < 1 || mo > 12) return null
  if (level === 'month') return `${y}-${pad(mo)}`
  if (!d || d < 1) return null
  const last = new Date(y, mo, 0).getDate()
  if (d > last) return null
  return `${y}-${pad(mo)}-${pad(d)}`
}

// ---------------------------------------------------------------------------- keywords

/** photokin's processing markers: shown apart from the photo's own keywords. */
export function isMarkerKeyword(kw: string): boolean {
  const k = kw.trim().toLowerCase()
  return k.startsWith('date:') || k.endsWith(' analyzed') || k === 'back' || k === 'negative'
}

export function isDateMarker(kw: string): boolean {
  return /^\s*date:/i.test(kw)
}

/** Keywords typed or pasted: split on commas, semicolons and line breaks; trimmed; no repeats. */
export function splitKeywords(text: string): string[] {
  return text.split(/[,;\n]/).map((k) => k.trim()).filter(Boolean)
}

export function addKeywords(list: string[], add: string[]): string[] {
  const seen = new Set(list.map((k) => k.toLowerCase()))
  const out = [...list]
  for (const k of add) {
    if (!k || isDateMarker(k) || seen.has(k.toLowerCase())) continue
    seen.add(k.toLowerCase())
    out.push(k)
  }
  return out
}

/** Lightroom-style files list each tagged person as a keyword too. When this file does (a face name
 *  from before the change is a keyword), a rename or removal is reflected in the keywords; otherwise
 *  null. `before` / `after`: the face names before and after the change. */
export function peopleKeywords(keywords: string[], before: string[], after: string[], oldName: string, newName: string): string[] | null {
  const lower = new Set(keywords.map((k) => k.toLowerCase()))
  if (!before.some((n) => n && lower.has(n.toLowerCase()))) return null
  let out = keywords
  const stillUsed = after.some((n) => n.toLowerCase() === oldName.toLowerCase())
  if (oldName && !stillUsed) out = out.filter((k) => k.toLowerCase() !== oldName.toLowerCase())
  if (newName && !out.some((k) => k.toLowerCase() === newName.toLowerCase())) out = [...out, newName]
  return out.length === keywords.length && out.every((k, i) => k === keywords[i]) ? null : out
}

// ---------------------------------------------------------------------------- stale drafts

function iou(a: Box | null, b: Box | null): number {
  if (!a || !b) return 0
  const ix = Math.max(0, Math.min(a[0] + a[2], b[0] + b[2]) - Math.max(a[0], b[0]))
  const iy = Math.max(0, Math.min(a[1] + a[3], b[1] + b[3]) - Math.max(a[1], b[1]))
  const inter = ix * iy
  const uni = a[2] * a[3] + b[2] * b[3] - inter
  return uni > 0 ? inter / uni : 0
}

/** Face edits made for another version of the file, moved onto this version's faces: each edit
 *  goes to the face with the same name and (nearly) the same box it was made on; edits whose face
 *  is gone are dropped. New faces stay. Returns the edits and how many were dropped. */
export function rematchFaces(edits: Record<string, FaceEdit> | undefined, meta: PhotoMeta): { faces: Record<string, FaceEdit>; dropped: number } {
  const out: Record<string, FaceEdit> = {}
  let dropped = 0
  const base = [...meta.faces.named, ...meta.faces.unnamed]
  const used = new Set<string>()
  for (const [k, e] of Object.entries(edits || {})) {
    if (k.startsWith('new:')) {
      out[k] = e
      continue
    }
    const same = base.find((f) => f.key === k && f.name === (e.was?.name ?? f.name))
    const hit = same ?? (e.was ? base.find((f) => f.key && !used.has(f.key) && f.name === e.was!.name && (e.was!.box === null ? f.box === null : iou(f.box, e.was!.box) > 0.8)) : undefined)
    if (hit?.key && !used.has(hit.key)) {
      used.add(hit.key)
      out[hit.key] = e
    } else dropped++
  }
  return { faces: out, dropped }
}

// ---------------------------------------------------------------------------- name suggestions

const RECENT_KEY = 'recentNames'
const RECENT_MAX = 300

export function recentNames(): string[] {
  try {
    const v = JSON.parse(loadPref(RECENT_KEY) || '[]')
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function rememberName(name: string) {
  const n = name.trim()
  if (!n) return
  const list = [n, ...recentNames().filter((x) => x.toLowerCase() !== n.toLowerCase())].slice(0, RECENT_MAX)
  savePref(RECENT_KEY, JSON.stringify(list))
}

/** Names matching what was typed (any word starts with it), recent and nearby names first. */
export function suggestNames(typed: string, pools: string[][], limit = 6): string[] {
  const q = typed.trim().toLowerCase()
  const seen = new Set<string>()
  const out: string[] = []
  for (const pool of pools) {
    for (const n of pool) {
      const k = n.trim()
      if (!k || seen.has(k.toLowerCase())) continue
      const words = k.toLowerCase().split(/[\s\-'’.]+/)
      if (q && !(k.toLowerCase().startsWith(q) || words.some((w) => w.startsWith(q)))) continue
      if (k.toLowerCase() === q) continue
      seen.add(k.toLowerCase())
      out.push(k)
      if (out.length >= limit) return out
    }
  }
  return out
}
