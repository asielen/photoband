// Edited photo details (the draft's `meta`): the shapes the backend's metaedit.py takes, and the
// small pure helpers the editor needs right away (faces on screen, the date editor's level, the
// keyword chips, name suggestions). The backend applies the same edits for captions and saving.

import type { Face, PhotoMeta } from './types'
import { loadPref, savePref } from './prefs'

export type Box = [number, number, number, number]
/** How much of a date is known: the day, the month or the year. */
export type DateLevel = 'day' | 'month' | 'year'

export interface FaceEdit {
  name?: string
  box?: Box | null
  deleted?: boolean
  /** the face as the file had it when the edit started (to find it again in a changed file) */
  was?: { name: string; box: Box | null }
}

export interface DateEdit {
  iso: string // '1952' | '1952-06' | '1952-06-14'
  /** ('circa' in older drafts: an estimated year) */
  level: DateLevel | 'circa'
  /** the finest part known is a best guess (summer 1944 -> July, around Thanksgiving -> the 23rd): printed "c." */
  estimate?: boolean
  /** the file's own photokin pattern, kept while only the values are edited (a known birthday with a
   *  guessed year stays "Y~M!D!"); none: the level's own (Y!M!D~, Y!M~, Y~ or the exact ones) */
  pattern?: string
}

export type NormDate = { iso: string; level: DateLevel; estimate: boolean; pattern?: string }

/** A date edit in today's shape (an older draft's 'circa' is an estimated year; a pattern that is
 *  just the level's own is left out, so equal dates compare equal). */
export function normDate(d: DateEdit): NormDate {
  const n: NormDate = d.level === 'circa' ? { iso: d.iso, level: 'year', estimate: true } : { iso: d.iso, level: d.level, estimate: !!d.estimate }
  const p = d.pattern?.toUpperCase()
  if (p && d.level !== 'circa' && p !== datePattern(n.level, n.estimate)) n.pattern = p
  return n
}

/** Same date, level, guess and pattern? */
export function sameDate(a: DateEdit | null | undefined, b: DateEdit | null | undefined): boolean {
  if (!a || !b) return !a && !b
  return JSON.stringify(normDate(a)) === JSON.stringify(normDate(b))
}

/** Which part of a pattern is the guess, in words, when it is not the finest one shown ("the
 *  year" for a known birthday with a guessed year); '' otherwise. */
export function guessedPart(pattern: string | undefined, level: DateLevel): string {
  const m = /^Y([!?~@])(?:M([!?~@]))?(?:D([!?~@]))?$/.exec((pattern || '').toUpperCase())
  if (!m) return ''
  const g = (c?: string) => c === '~' || c === '@'
  const parts = [g(m[1]) ? 'year' : '', g(m[2]) ? 'month' : '', g(m[3]) ? 'day' : ''].filter(Boolean)
  return parts.length && !(parts.length === 1 && parts[0] === level) ? `the ${parts.join(' and ')}` : ''
}

export interface MetaEdits {
  title?: string
  caption?: string
  notes?: string
  /** the photographers, one name per entry (a text from an older draft is one name) */
  creator?: string | string[]
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

/** photokin's "DATE:" pattern for a level of detail (the backend writes it). */
export function datePattern(level: DateLevel, estimate: boolean): string {
  return { day: estimate ? 'Y!M!D~' : 'Y!M!D!', month: estimate ? 'Y!M~' : 'Y!M!', year: estimate ? 'Y~' : 'Y!' }[level]
}

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
  // a face with neither a name nor a place on the photo (a cleared PersonInImage name) can't be
  // stored anywhere: it is gone (as the backend sees it)
  const kept = out.filter((f) => f.name.trim() || f.box)
  return { named: kept.filter((f) => f.name.trim()), unnamed: kept.filter((f) => !f.name.trim()) }
}

/** Left-to-right order of positioned faces (the caption's order). */
export function leftToRight(faces: Face[]): Face[] {
  return faces.filter((f) => f.box).sort((a, b) => a.box![0] + a.box![2] / 2 - (b.box![0] + b.box![2] / 2))
}

export function newFaceKey(): string {
  return 'new:' + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4)
}

// ---------------------------------------------------------------------------- dates

export type DateState =
  | { kind: 'none' }
  | { kind: 'date'; iso: string; level: DateLevel; estimate: boolean; pattern?: string }
  | { kind: 'text'; text: string; year: number | null; month?: number; day?: number }

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
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
  let estimate = false
  const pm = /^Y([!?~@])(?:M([!?~@]))?(?:D([!?~@]))?$/.exec(pattern)
  if (pm) {
    // as the backend reads photokin's patterns: each part rated known ("!") or a guess ("~", "@")
    // is kept while the coarser ones are; an unknown ("?") or unrated part is left out
    const known = (c: string | undefined) => c === '!' || c === '~' || c === '@'
    const [, py, pmo, pd] = pm
    const monthKept = known(pmo) && !!mo
    const dayKept = monthKept && known(pd) && !!d
    if (py === '?') {
      // the year is unknown (a birthday): the editor can't hold that, so it is shown as words
      if (!monthKept) return { kind: 'none' }
      return { kind: 'text', text: `${MONTH_NAMES[mo - 1]}${dayKept ? ` ${d}` : ''} (year unknown)`, year: null,
        month: mo, ...(dayKept ? { day: d } : {}) }
    }
    level = dayKept ? 'day' : monthKept ? 'month' : 'year'
    estimate = py !== '!' || (monthKept && pmo !== '!') || (dayKept && pd !== '!')
    const iso0 = level === 'day' ? `${y}-${pad(mo)}-${pad(d)}` : level === 'month' ? `${y}-${pad(mo)}` : `${y}`
    // the file's own pattern goes along (kept while only the values are edited)
    return { kind: 'date', ...normDate({ iso: iso0, level, estimate, pattern }) }
  }
  const iso = level === 'day' ? `${y}-${pad(mo)}-${pad(d)}` : level === 'month' ? `${y}-${pad(mo || 6)}` : `${y}`
  return { kind: 'date', iso, level, estimate }
}

/** An ISO date for a level from year / month / day inputs, or null when they don't make one. */
export function isoFor(level: DateLevel, y: number | null, mo: number | null, d: number | null): string | null {
  if (!y || !Number.isInteger(y) || y < 1000 || y > new Date().getFullYear() + 1) return null
  if (level === 'year') return String(y)
  if (!mo || !Number.isInteger(mo) || mo < 1 || mo > 12) return null
  if (level === 'month') return `${y}-${pad(mo)}`
  if (!d || !Number.isInteger(d) || d < 1) return null
  const last = new Date(y, mo, 0).getDate()
  if (d > last) return null
  return `${y}-${pad(mo)}-${pad(d)}`
}

// ---------------------------------------------------------------------------- keywords

/** photokin's processing markers: shown apart from the photo's own keywords. */
export function isMarkerKeyword(kw: string): boolean {
  const k = kw.trim().toLowerCase()
  // (photokin takes any keyword starting "DATE:" for its date marker; only well-formed ones are
  // the date editor's, see isDateMarker)
  return k.startsWith('date:') || k.endsWith(' analyzed') || k === 'back' || k === 'negative'
}

/** photokin's date-certainty keyword ("DATE: Y!M~"), and only that: "Date: ask Ann" is a keyword. */
export function isDateMarker(kw: string): boolean {
  return /^\s*DATE:\s*Y[!?~@](M[!?~@])?(D[!?~@])?\s*$/i.test(kw)
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

/** Lightroom-style files list each tagged person as a keyword too. In such a file (`asKeywords`,
 *  decided from the file itself), a renamed, added or removed name is reflected in the keywords;
 *  otherwise null (no change). `after`: the face names after the change; `removable`: the old name's
 *  keyword may go (it is there for this face, or was added by these edits, not a keyword of its own). */
export function peopleKeywords(keywords: string[], asKeywords: boolean, after: string[], oldName: string, newName: string, removable: boolean): string[] | null {
  if (!asKeywords) return null
  let out = keywords
  const stillUsed = after.some((n) => n.toLowerCase() === oldName.toLowerCase())
  if (oldName && !stillUsed && removable) out = out.filter((k) => k.toLowerCase() !== oldName.toLowerCase())
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
      // a face added here that the file has meanwhile (this edit, already written): not again
      const written = !!e.box && base.some((f) => f.name === (e.name ?? '') && iou(f.box, e.box!) > 0.8)
      if (written) dropped++
      else out[k] = e
      continue
    }
    const same = base.find((f) => f.key === k && f.name === (e.was?.name ?? f.name) &&
      (!e.was || (e.was.box === null ? f.box === null : iou(f.box, e.was.box) > 0.8)))
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

// ---------------------------------------------------------------------------- text and saving

// as the backend's metaedit.validate: no control characters (a pasted vertical tab, a NUL), line
// breaks only where a field can hold them, and line endings as they are stored
const CTRL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g

export function cleanText(v: string, multiline: boolean): string {
  let t = v.replace(/\r\n?/g, '\n').replace(/[\u000b\u000c\u0085\u2028\u2029]/g, multiline ? '\n' : ' ').replace(CTRL, '')
  // a lone half of a surrogate pair can't be stored
  t = t.replace(/[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/g, '')
  if (!multiline) t = t.replace(/\n/g, ' ')
  return t
}

/** A problem with a text detail the backend would refuse, or ''. */
export function detailProblem(v: string): string {
  if (/^\s*base64:/i.test(v)) return 'Can’t start with “base64:” (it would be read as binary data).'
  return ''
}

/** The edits made after `sent` was taken (while a save wrote `sent`): every detail that differs
 *  from what was sent, including one reset meanwhile (absent now, sent then): the file now has the
 *  sent value, so going back takes an edit to the value from before the save (`before(k)`). Face
 *  edits made meanwhile are not kept (the file's faces were just renumbered by the write):
 *  `droppedFaces` says so. */
export function editsSince(cur: MetaEdits | undefined, sent: MetaEdits | undefined,
  before: (k: keyof MetaEdits) => unknown = () => undefined): { meta: MetaEdits | undefined; droppedFaces: boolean } {
  const out: MetaEdits = {}
  let droppedFaces = false
  const keys = new Set([...Object.keys(cur || {}), ...Object.keys(sent || {})]) as Set<keyof MetaEdits>
  for (const k of keys) {
    const has = !!cur && k in cur
    if (has && JSON.stringify(cur![k]) === JSON.stringify(sent?.[k])) continue
    if (k === 'faces') {
      droppedFaces = true
      continue
    }
    if (has) (out as any)[k] = cur![k]
    else {
      const v = before(k)
      if (v !== undefined) (out as any)[k] = v
    }
  }
  return { meta: Object.keys(out).length ? out : undefined, droppedFaces }
}
