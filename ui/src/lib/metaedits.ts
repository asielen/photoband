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
  /** photokin's pattern when it is not the level's own: the file's, kept while only the values are
   *  edited (a known birthday with a guessed year stays "Y~M!D!"), one for a date whose year is
   *  unknown ("Y?M!D!": the year in iso is a placeholder), or one typed under Advanced. It says
   *  what is a guess (estimate follows it). none: the level's own (Y!M!D~, Y!M~, Y~ or exact ones) */
  pattern?: string
}

export type NormDate = { iso: string; level: DateLevel; estimate: boolean; pattern?: string }

const PATTERN_RE = /^Y([!?~@])(?:M([!?~@]))?(?:D([!?~@]))?$/
const RANK: Record<DateLevel, number> = { year: 1, month: 2, day: 3 }

/** What a photokin pattern keeps of a date of this level: its guessed parts ('y', 'm', 'd'; '-y':
 *  the year is unknown), or null when it keeps nothing or a part the date doesn't have. As the
 *  backend's metaedit._pattern_parts (and captiontokens' certainty_parts): a part rated known ("!")
 *  or a guess ("~", "@") is kept while the coarser ones are; "?" or unrated is left out. */
export function patternParts(pattern: string | undefined, level: DateLevel): Set<string> | null {
  const m = PATTERN_RE.exec((pattern || '').trim().toUpperCase())
  if (!m) return null
  const known = (c?: string) => c === '!' || c === '~' || c === '@'
  const guess = (c?: string) => c === '~' || c === '@'
  const [, y, mo, d] = m
  const g = new Set<string>()
  if (y === '?') g.add('-y')
  else if (guess(y)) g.add('y')
  const keepM = known(mo)
  const keepD = keepM && known(d)
  if (!keepM && y === '?') return null
  const finest: DateLevel = keepD ? 'day' : keepM ? 'month' : 'year'
  if (RANK[finest] > RANK[level]) return null
  if (keepM && level !== 'year' && guess(mo)) g.add('m')
  if (keepD && level === 'day' && guess(d)) g.add('d')
  return g
}

/** A date edit in today's shape (an older draft's 'circa' is an estimated year; a pattern says what
 *  is a guess; a pattern that is just the level's own, or no longer describes the date, is left
 *  out), exactly as the backend's validate makes it, so equal dates compare equal. */
export function normDate(d: DateEdit): NormDate {
  const level: DateLevel = d.level === 'circa' ? 'year' : d.level
  const n: NormDate = { iso: d.iso, level, estimate: d.level === 'circa' || !!d.estimate }
  const p = d.pattern?.trim().toUpperCase()
  const g = p ? patternParts(p, level) : null
  if (p && g) {
    n.estimate = [...g].some((x) => x !== '-y')
    if (p !== datePattern(n.level, n.estimate)) n.pattern = p
  }
  return n
}

/** Same date, level, guess and pattern? */
export function sameDate(a: DateEdit | null | undefined, b: DateEdit | null | undefined): boolean {
  if (!a || !b) return !a && !b
  return JSON.stringify(normDate(a)) === JSON.stringify(normDate(b))
}

/** Which parts of a date are the guess, in words ("the day", "the year and day"); '' for none. */
export function guessedPart(d: NormDate): string {
  const g = d.pattern ? patternParts(d.pattern, d.level) : d.estimate ? new Set([d.level[0]]) : null
  if (!g) return ''
  const parts = [g.has('y') ? 'year' : '', g.has('m') ? 'month' : '', g.has('d') ? 'day' : ''].filter(Boolean)
  return parts.length ? `the ${parts.join(' and ')}` : ''
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
  /** stored: the whole date the file holds when it has more than iso keeps (a day marked unknown,
   *  photokin's filled-in June 15): shown under Advanced */
  | { kind: 'date'; iso: string; level: DateLevel; estimate: boolean; pattern?: string; stored?: string }
  | { kind: 'text'; text: string; year: number | null }

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
const DATE_RE = /^\s*(\d{4})(?:[-:/.](\d{1,2})(?:[-:/.](\d{1,2}))?)?(?=$|[\sT])(.*)$/

function pad(n: number) {
  return String(n).padStart(2, '0')
}

function isoOf(y: number, mo: number | null, d: number | null): string {
  return mo ? (d ? `${y}-${pad(mo)}-${pad(d)}` : `${y}-${pad(mo)}`) : `${y}`
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
  const whole = isoOf(y, mo || null, d || null)
  const pattern = String(fields?.date_certainty ?? '').toUpperCase()
  const pm = PATTERN_RE.exec(pattern)
  if (pm) {
    // as the backend reads photokin's patterns: each part rated known ("!") or a guess ("~", "@")
    // is kept while the coarser ones are; an unknown ("?") or unrated part is left out
    const known = (c: string | undefined) => c === '!' || c === '~' || c === '@'
    const [, py, pmo, pd] = pm
    const monthKept = known(pmo) && !!mo
    const dayKept = monthKept && known(pd) && !!d
    if (py === '?' && !monthKept) return { kind: 'none' }   // nothing is known
    const level: DateLevel = dayKept ? 'day' : monthKept ? 'month' : 'year'
    const iso = isoOf(y, monthKept ? mo : null, dayKept ? d : null)
    // the file's own pattern goes along (kept while only the values are edited)
    return { kind: 'date', ...normDate({ iso, level, pattern }), ...(whole !== iso ? { stored: whole } : {}) }
  }
  const level: DateLevel = d ? 'day' : mo ? 'month' : 'year'
  return { kind: 'date', iso: whole, level, estimate: false }
}

/** The Year / Month / Day boxes for a date: a part the pattern doesn't keep (a day marked unknown,
 *  the year of a birthday) is blank. */
export function dateBoxes(d: NormDate): { year: number | null; month: number | null; day: number | null } {
  const [y, mo, dd] = d.iso.split('-').map(Number)
  const m = d.pattern ? PATTERN_RE.exec(d.pattern) : null
  const known = (c?: string) => c === '!' || c === '~' || c === '@'
  const keepM = !m || known(m[2])
  const keepD = keepM && (!m || known(m[3]))
  return { year: m?.[1] === '?' ? null : y, month: keepM ? mo || null : null, day: keepD ? dd || null : null }
}

const YEAR_MAX = () => new Date().getFullYear() + 1
const leap = (y: number) => (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0

/** The date the Year / Month / Day boxes make: blank is unknown, Estimated makes the finest part
 *  filled in a guess. `keep`: the date as shown, whose pattern (a guessed year with a known
 *  birthday) and placeholder year stay while the same boxes are filled and Estimated is as it was.
 *  { edit: null }: all blank (no date). { problem }: the boxes don't make a date yet. */
export function editFromBoxes(year: number | null, month: number | null, day: number | null, estimate: boolean,
  keep?: NormDate | null): { edit: NormDate | null } | { problem: string } {
  if (year === null && month === null && day === null) return { edit: null }
  if (year !== null && (!Number.isInteger(year) || year < 1000 || year > YEAR_MAX())) return { problem: `Enter a year between 1000 and ${YEAR_MAX()}, or leave it blank.` }
  if (day !== null && month === null) return { problem: 'A day needs its month.' }
  if (month !== null && (!Number.isInteger(month) || month < 1 || month > 12)) return { problem: 'Pick a month.' }
  // the year a date without one is stored with (EXIF needs a whole date): the one the date had,
  // else 1900 (1904 for February 29)
  let y = year
  if (y === null) {
    const had = keep ? +keep.iso.slice(0, 4) : NaN
    y = Number.isInteger(had) && had >= 1000 ? had : 1900
    if (month === 2 && day === 29 && !leap(y)) y = 1904
  }
  if (day !== null) {
    const last = new Date(y, month!, 0).getDate()
    if (!Number.isInteger(day) || day < 1 || day > last) return { problem: `${MONTH_NAMES[month! - 1]} has ${last} days.` }
  }
  const level: DateLevel = day !== null ? 'day' : month !== null ? 'month' : 'year'
  const iso = isoOf(y, month, day)
  if (keep?.pattern && keep.estimate === estimate) {
    const kb = dateBoxes(keep)
    const same = (kb.year === null) === (year === null) && (kb.month !== null) === (month !== null) && (kb.day !== null) === (day !== null)
    if (same && patternParts(keep.pattern, level)) return { edit: normDate({ iso, level, pattern: keep.pattern }) }
  }
  const p = (year === null ? 'Y?' : `Y${estimate && level === 'year' ? '~' : '!'}`) +
    (month !== null ? `M${estimate && level === 'month' ? '~' : '!'}` : '') + (day !== null ? `D${estimate ? '~' : '!'}` : '')
  return { edit: normDate({ iso, level, estimate, pattern: p }) }
}

/** The date typed under Advanced: the stored date (YYYY, YYYY-MM or YYYY-MM-DD) and photokin's
 *  keyword pattern ("Y!M~", with or without "DATE:"; blank: the stored date known to its last
 *  part). { edit: null }: both blank. */
export function parseAdvanced(stored: string, keyword: string): { edit: NormDate | null } | { problem: string; field: 'stored' | 'keyword' } {
  const st = stored.trim()
  const kw = keyword.trim().replace(/^date:\s*/i, '').toUpperCase()
  if (!st && !kw) return { edit: null }
  const m = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/.exec(st)
  if (!m) return { problem: 'Write the stored date as YYYY, YYYY-MM or YYYY-MM-DD.', field: 'stored' }
  const [y, mo, d] = [+m[1], m[2] ? +m[2] : null, m[3] ? +m[3] : null]
  if (y < 1000 || y > YEAR_MAX()) return { problem: `The stored year must be between 1000 and ${YEAR_MAX()}.`, field: 'stored' }
  if (mo !== null && (mo < 1 || mo > 12)) return { problem: `${st} is not a date.`, field: 'stored' }
  if (d !== null && (d < 1 || d > new Date(y, mo!, 0).getDate())) return { problem: `${st} is not a date.`, field: 'stored' }
  const level: DateLevel = d !== null ? 'day' : mo !== null ? 'month' : 'year'
  if (!kw) return { edit: normDate({ iso: st, level, estimate: false }) }
  if (!PATTERN_RE.test(kw)) return { problem: 'Write the keyword like DATE: Y!M~ (Y, M, D, each with ! known, ~ a guess or ? unknown).', field: 'keyword' }
  if (!patternParts(kw, level)) return { problem: `DATE: ${kw} rates a part the stored date doesn't have (or says nothing is known).`, field: 'keyword' }
  return { edit: normDate({ iso: st, level, pattern: kw }) }
}

/** Is this the file's own date, as shown or as stored (the whole date under Advanced, its day
 *  marked unknown, typed back as it is)? Then it is no edit. */
export function isFileDate(d: DateEdit, fields: Record<string, any> | null | undefined): boolean {
  const st = dateState(fields)
  if (st.kind !== 'date') return false
  if (sameDate(d, st)) return true
  if (!st.stored) return false
  const level: DateLevel = st.stored.length > 7 ? 'day' : st.stored.length > 4 ? 'month' : 'year'
  return sameDate(d, { iso: st.stored, level, pattern: st.pattern || datePattern(st.level, st.estimate) })
}

/** The stored date and keyword of a date, as Advanced shows them. */
export function advancedOf(d: NormDate, stored?: string): { stored: string; keyword: string } {
  return { stored: stored || d.iso, keyword: `DATE: ${d.pattern || datePattern(d.level, d.estimate)}` }
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
