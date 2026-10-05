// Application state (Svelte 5 runes). One PhotoSession per opened photo holds its
// draft (edits not yet saved), undo history, resolved captions and current layout.

import { ApiError, get, post, postForm } from './api'
import { dialogs } from './dialogs.svelte'
import * as fontsModule from './fonts'
import { ensureFonts, loadRegistry, resolveFont } from './fonts'
import { clearMeasureCache, computeLayout, deepAssign, effectiveTemplate } from './layout'
import { plainText } from './markup'
import {
  addKeywords, cleanText, dateState, editsSince, effectiveFaces, hasEdits, isDateMarker, newFaceKey, peopleKeywords, rematchFaces,
  rememberName, type Box, type DateEdit, type FaceEdit, type MetaEdits, type TextField,
} from './metaedits'
import { loadFlag, saveFlag } from './prefs'
import { renderTextTiles } from './render'
import type {
  BlockState, ExistingAnalysis, Face, LayoutResult, Overrides, PhotoDraft, PhotoMeta, Rect, Resolution,
  SaveResult, Settings, Template, Warning,
} from './types'

export type Status = 'untouched' | 'draft' | 'saved' | 'error'

export interface PhotoItem {
  path: string
  name: string
  status: Status
  error?: string
  draft?: boolean
}

export interface Toast {
  id: number
  kind: 'info' | 'success' | 'error' | 'warn'
  text: string
  /** a file name shown after the text, on one line with an ellipsis */
  file?: string
  action?: { label: string; run: () => void }
}

export class PhotoSession {
  path: string
  meta = $state<PhotoMeta | null>(null)
  existing = $state<ExistingAnalysis | null>(null)
  existingLoading = $state(false)
  /** resolves when the existing-caption check has finished */
  existingPromise: Promise<void> | null = null
  /** the user chose to keep the existing band as part of the photo ("Ignore" / "Save anyway") */
  existingIgnored = $state(false)
  /** the draft before a case B/C choice, restored by Cancel */
  preExisting: string | null = null
  // the template is set right away, so the inspector keeps its editors (and focus) while the photo loads
  draft = $state<PhotoDraft>({ templateId: '', overrides: {}, blocks: {}, mode: 'band', sourceRect: null, photoRect: null })
  resolved = $state<Record<string, Resolution>>({})
  layout = $state.raw<LayoutResult | null>(null)
  error = $state('')
  dirty = $state(false)
  undoStack: string[] = []
  redoStack: string[] = []
  lastSnap = ''
  /** the editor state when the photo had no unsaved edits (after opening or saving it) */
  cleanSnap = ''
  snapTimer: any = null
  draftTimer: any = null
  draftPendingSince = 0
  resolveTimer: any = null
  proxyVersion = $state(0)
  erasePreview = $state<string | null>(null)
  /** bumped by every relayout, so a slower earlier layout never replaces a newer one */
  layoutGen = 0
  /** the same for caption resolves (a slow reply for an earlier template never wins) */
  resolveGen = 0
  /** which template / file version `resolved` belongs to */
  resolvedFor = ''
  /** the same for existing-caption checks (a reload starts a new one) */
  existingGen = 0
  /** resolves when the photo has been opened (meta, draft, first layout) */
  loadPromise: Promise<void> | null = null
  /** replaced by a fresh session (after an overwrite): never autosaves again */
  retired = false
  constructor(path: string, templateId?: string) {
    this.path = path
    // (app is always initialized by the time sessions are created)
    this.draft.templateId = templateId ?? app.defaultTemplateId() ?? ''
  }
}

/** Short, stable hash of an editor state (identifies which draft a save covered). */
export function stateHash(str: string): string {
  let h1 = 0xdeadbeef ^ str.length
  let h2 = 0x41c6ce57 ^ str.length
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i)
    h1 = Math.imul(h1 ^ c, 2654435761)
    h2 = Math.imul(h2 ^ c, 1597334677)
  }
  h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909)
  h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909)
  return (h2 >>> 0).toString(16).padStart(8, '0') + (h1 >>> 0).toString(16).padStart(8, '0')
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

/** Everything in a draft that is tied to the pixels of one version of the file. */
const BAND_GEOMETRY = {
  mode: 'band' as const,
  sourceRect: null,
  photoRect: null,
  existingChoice: null,
  brushAdd: null,
  brushRemove: null,
  keepBand: false,
}
type Geometry = Pick<PhotoDraft, 'mode' | 'sourceRect' | 'photoRect' | 'existingChoice' | 'brushAdd' | 'brushRemove' | 'keepBand'>

/** Same version of the file? (size and modification time, as the backend's stat) */
export function sameFile(a: PhotoMeta['stat'] | null | undefined, b: PhotoMeta['stat'] | null | undefined): boolean {
  if (!a || !b || String(a[0]) !== String(b[0]) || String(a[1]) !== String(b[1])) return false
  // a same-size replacement whose modified time was kept is another file id (when both know it)
  const ia = a[3]
  const ib = b[3]
  return !ia || !ib || String(ia) === String(ib)
}

/** A stored draft as the editor (and a batch using drafts) may use it for this version of the file.
 *  The draft records the file version it was made for (`_stat`). When that differs from the file
 *  now (`stat`), only its text and style (templateId, overrides, blocks) are taken: its mode and
 *  photo edges belong to the old pixels, so they come from `base`. `_stat` and `_hash` never end
 *  up in the returned draft. Drafts without `_stat` (older versions) count as matching. */
export function draftForFile(
  stored: any,
  stat: PhotoMeta['stat'] | null | undefined,
  base: PhotoDraft,
  hasTemplate: (id: string) => boolean,
  meta?: PhotoMeta | null,
): { draft: PhotoDraft; hash: string | null; stale: boolean; droppedFaces: number } {
  const { _hash, _stat, _size, ...d } = (stored || {}) as PhotoDraft & { _hash?: string; _stat?: PhotoMeta['stat']; _size?: [number, number] }
  const templateId = d.templateId && hasTemplate(d.templateId) ? d.templateId : base.templateId
  const stale = !!_stat && !sameFile(_stat, stat)
  let droppedFaces = 0
  let draft: PhotoDraft
  // (a draft missing a field the editor checks, e.g. one written by the server, gets the base's)
  if (!stale) draft = { ...base, ...d, templateId }
  else {
    // edited details are kept (they don't depend on the pixels); face edits only where their face
    // is found again in this version, and only when the photo still has the same shape
    const m = d.meta ? { ...d.meta } : undefined
    if (m?.faces) {
      const info = meta?.info
      const sameShape = !!info && (!_size || (_size[0] === info.upright_width && _size[1] === info.upright_height))
      const r = meta && sameShape ? rematchFaces(m.faces, meta) : { faces: {}, dropped: Object.keys(m.faces).length }
      droppedFaces = r.dropped
      if (Object.keys(r.faces).length) m.faces = r.faces
      else delete m.faces
    }
    draft = { ...base, templateId, overrides: d.overrides || {}, blocks: d.blocks || {}, meta: m, faceRows: d.faceRows ?? null }
  }
  return { draft, hash: _hash ?? null, stale, droppedFaces }
}

/** The parts of a draft that are not about the caption's geometry: kept when the draft is rebuilt. */
function keepDetails(d: PhotoDraft): Pick<PhotoDraft, 'meta' | 'faceRows'> {
  return { meta: d.meta, faceRows: d.faceRows }
}

/** Why Overwrite original is off for a scan with a physical caption (case C). */
export const CASE_C_OVERWRITE_OFF = 'This scan has a handwritten or printed caption, so overwriting it is turned off. Save a copy instead, or allow it in Settings › Saving.'

function clone<T>(v: T): T {
  return JSON.parse(JSON.stringify(v))
}

class AppStore {
  ready = $state(false)
  fatal = $state('')
  version = $state('')
  platform = $state('')
  mode = $state<'desktop' | 'browser'>('browser')
  notice = $state('')
  ocrEngines = $state<string[]>([])
  settings = $state<Settings>(null as any)
  templates = $state<Template[]>([])
  tokens = $state<any[]>([])
  photos = $state<PhotoItem[]>([])
  current = $state(-1)
  sessions = new Map<string, PhotoSession>()
  session = $state<PhotoSession | null>(null)
  toasts = $state<Toast[]>([])
  view = $state<'editor' | 'batch'>('editor')
  /** "Don't ask again" for Overwrite this session, and the backup setting it was given under */
  overwriteConfirmed: false | 'backup' | 'nobackup' = false
  saving = $state(false)
  /** which kind of save is running, for the toolbar's spinner */
  savingMode = $state<'copy' | 'overwrite' | null>(null)
  private _showFaces = $state(loadFlag('showFaces'))
  /** face boxes on the photo (remembered) */
  get showFaces() {
    return this._showFaces
  }
  set showFaces(v: boolean) {
    this._showFaces = v
    saveFlag('showFaces', v)
    if (!v) {
      this.faceTool = 'select'
      this.faceToolTarget = null
      this.selectedFace = null
    }
  }
  /** the face editor: 'add' draws a new box with the next drag */
  faceTool = $state<'select' | 'add'>('select')
  /** the face the next drawn box is for (a name that has no place on the photo yet) */
  faceToolTarget = $state<string | null>(null)
  /** the face selected on the photo (its key), and the one hovered here or in the People list */
  selectedFace = $state<string | null>(null)
  hoverFace = $state<string | null>(null)
  showOriginal = $state(false)
  incompleteBatches = $state<any[]>([])
  fontsVersion = $state(0)
  focusBlock = $state<string | null>(null)
  /** the caption block that last had focus (restored after Save & next) */
  lastBlock: string | null = null
  /** Tab (with nothing focused) hides the filmstrip and inspector */
  panelsHidden = $state(false)
  inspectorTab = $state<'text' | 'style' | 'layout' | 'metadata'>('text')
  batchReview = $state<{ paths: string[]; onExit: () => void } | null>(null)
  /** the command palette (Mod+K) */
  paletteOpen = $state(false)
  /** a disclosure a warning asked to show ("style-more", "layout-more"); the Inspector opens it */
  revealSection = $state<string | null>(null)
  private toastId = 0

  /** Show an Inspector control that may sit behind "More options". */
  reveal(tab: 'text' | 'style' | 'layout' | 'metadata', section: string | null = null) {
    this.panelsHidden = false
    this.inspectorTab = tab
    this.revealSection = section
  }

  // ------------------------------------------------------------------ boot
  async init() {
    try {
      const st = await get('/api/state')
      this.version = st.version
      this.platform = st.platform
      this.mode = st.mode
      this.notice = st.notice || ''
      this.settings = st.settings
      this.ocrEngines = st.ocrEngines
      this.incompleteBatches = st.incompleteBatches || []
      if (!st.exiftool) this.toast('error', st.exiftoolError || 'ExifTool was not found. Metadata cannot be read or written.')
      const [tpls, toks] = await Promise.all([get<Template[]>('/api/templates'), get('/api/tokens'), loadRegistry()])
      this.templates = tpls
      this.tokens = toks
      this.applyTheme()
      this.watchFonts()
      this.watchUnload()
      this.ready = true
    } catch (e: any) {
      this.fatal = e?.message || String(e)
    }
  }

  /** Pending drafts are written when the window is hidden or closed (and on request of the desktop shell). */
  private watchUnload() {
    if (typeof window === 'undefined') return
    const flush = () => { this.flushAll() }
    window.addEventListener('pagehide', flush)
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'hidden') flush()
    })
    ;(window as any).__photobandFlush = () => this.flushAll()
  }

  applyTheme() {
    const t = this.settings?.general?.theme || 'system'
    document.documentElement.dataset.theme = t
  }

  /** Errors stay until dismissed; other toasts close after `ms` (0 = sticky). */
  toast(kind: Toast['kind'], text: string, action?: Toast['action'], ms = 5000, file?: string) {
    // the same message twice in a row just refreshes the existing toast
    const dup = this.toasts.find((t) => t.text === text && t.kind === kind && t.file === file)
    if (dup) this.dismiss(dup.id)
    const id = ++this.toastId
    this.toasts = [...this.toasts, { id, kind, text, action, file }].slice(-4)
    if (ms > 0 && kind !== 'error') setTimeout(() => this.dismiss(id), ms)
  }
  dismiss(id: number) {
    this.toasts = this.toasts.filter((t) => t.id !== id)
  }

  async saveSettings(patch: any) {
    this.settings = await post('/api/settings', patch)
    this.applyTheme()
  }

  /** Templates that came embedded in an opened photo and aren't installed, by their own id
   *  (original id + content hash, so two versions of one template stay apart). They survive
   *  reloading the installed templates, so re-captioning such a photo keeps its template. */
  private fileTemplates = new Map<string, Template>()

  async reloadTemplates() {
    const installed = await get<Template[]>('/api/templates')
    this.templates = [...installed, ...this.fileTemplates.values()]
  }

  /** The template to use for a template embedded in a photo's record (installed one if it exists). */
  private adoptFileTemplate(t: Template): string {
    const installed = this.templates.find((x) => x.id === t.id && !x.fromFile)
    if (installed) return installed.id
    const { fromFile: _f, ...clean } = t as Template
    const hash = stateHash(JSON.stringify({ ...clean, name: '', builtin: false })).slice(0, 10)
    const id = `${t.id}~file-${hash}`
    if (!this.fileTemplates.has(id)) {
      // read-only in Settings (like a built-in); saved back to the file under its own id and name
      const ft: Template = { ...clean, id, builtin: true, name: t.name + ' (from file)', fromFile: { id: t.id, name: t.name, hash } }
      this.fileTemplates.set(id, ft)
    }
    if (!this.templates.some((x) => x.id === id)) this.templates = [...this.templates, this.fileTemplates.get(id)!]
    return id
  }

  /** The template as it is written into the photo's record (a file template under its original id). */
  recordTemplate(t: Template | undefined): Template | undefined {
    if (!t?.fromFile) return t
    const { fromFile, ...rest } = t
    return { ...rest, id: fromFile.id, name: fromFile.name, builtin: false }
  }

  template(id: string): Template | undefined {
    return this.templates.find((t) => t.id === id)
  }

  defaultTemplateId(): string {
    const want = this.settings?.session?.lastTemplate || this.settings?.general?.defaultTemplate
    return this.template(want) ? want : this.templates[0]?.id
  }

  // ------------------------------------------------------------------ opening
  async openPaths(paths: string[], includeSubfolders = false) {
    if (!paths.length) return
    try {
      const items = await post<{ path: string; name: string; draft: boolean }[]>('/api/open', { paths, includeSubfolders })
      if (!items.length) {
        this.toast('warn', 'No TIFF, JPEG or PNG files were found there.')
        return
      }
      const known = new Set(this.photos.map((p) => p.path))
      const added = items.filter((i) => !known.has(i.path)).map((i) => ({ path: i.path, name: i.name, status: (i.draft ? 'draft' : 'untouched') as Status, draft: i.draft }))
      this.photos = [...this.photos, ...added].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }))
      const first = added[0]?.path ?? items[0].path
      await this.select(this.photos.findIndex((p) => p.path === first))
    } catch (e: any) {
      this.toast('error', e.message)
    }
  }

  /** Clears the editor's photos (pending drafts are written first). No questions asked: see closeAllPhotos. */
  closeAll() {
    this.flushAll()
    this.photos = []
    this.sessions.clear()
    this.session = null
    this.current = -1
  }

  /** "Close all": edits not saved to a file are kept as drafts, but say so before closing. */
  async closeAllPhotos(): Promise<boolean> {
    await this.flushAll()
    const unsaved = [...this.sessions.values()].filter((s) => s.dirty && !s.retired && !(s as any).__batch)
    if (unsaved.length && !this.batchReview) {
      const names = unsaved.map((s) => s.meta?.name || basename(s.path))
      const list = names.slice(0, 6).map((n) => '• ' + n).join('\n') + (names.length > 6 ? `\n… and ${names.length - 6} more` : '')
      const one = unsaved.length === 1
      const r = await dialogs.ask(
        one ? 'Close with unsaved edits?' : `Close ${unsaved.length} photos with unsaved edits?`,
        `${one ? 'This photo has' : 'These photos have'} edits that are not saved to the file yet:\n${list}\n\n` +
          `The edits are kept as ${one ? 'a draft' : 'drafts'} and come back when you open ${one ? 'the photo' : 'these photos'} again.`,
        [
          { id: 'cancel', label: 'Cancel' },
          { id: 'ok', label: 'Close all', kind: 'primary' },
        ],
      )
      if (r.id !== 'ok') return false
    }
    this.closeAll()
    return true
  }

  async select(i: number, force = false) {
    if (i < 0 || i >= this.photos.length) return
    if (i === this.current && !force) return
    // unsaved edits are kept as a draft (autosaved): say so when leaving a photo that has them
    const cur = this.session
    if (cur) {
      this.flushDraft(cur)
      if (cur.dirty && !cur.retired && !this.batchReview && !(cur as any).__batch && this.photos[i]?.path !== cur.path) {
        const name = cur.meta?.name || basename(cur.path)
        this.toast('info', `Edits to ${name} are kept as a draft — not yet saved to the file.`, undefined, 4000)
      }
    }
    this.current = i
    const item = this.photos[i]
    let s = this.sessions.get(item.path)
    if (!s) {
      s = new PhotoSession(item.path, this.defaultTemplateId())
      this.sessions.set(item.path, s)
    }
    const fresh = !s.meta
    const work = fresh ? this.loadSession(s) : this.relayout(s)
    // Keep showing the previous photo for a moment while this one loads, so the
    // inspector doesn't flash empty (and focus stays in the editor); slow files switch anyway.
    if (fresh) await Promise.race([work, sleep(400)])
    if (this.current === i) this.session = s
    await work
    if (!fresh) this.checkChanged(s)
    this.prefetchNeighbors()
  }

  /** The file changed on disk since it was opened (another app edited it): re-read it, keep the text. */
  async checkChanged(s: PhotoSession) {
    if (!s.meta) return
    try {
      const meta = await get<PhotoMeta>('/api/photo/meta', { path: s.path })
      if (!sameFile(s.meta?.stat, meta.stat)) {
        await this.applyMeta(s, meta)
        this.toast('info', 'This photo changed on disk and was reloaded. Your caption text is kept; the photo edge and any choices about an existing caption start over.', undefined, 8000, s.meta?.name)
      }
    } catch (e) {
      /* not critical: the save re-checks */
      console.debug('checkChanged failed', e)
    }
  }

  /** Re-read the photo from disk (after it changed); the caption text is kept. */
  async reloadPhoto(s: PhotoSession) {
    try {
      const meta = await get<PhotoMeta>('/api/photo/meta', { path: s.path })
      await this.applyMeta(s, meta)
      this.toast('success', 'Reloaded the photo from disk. Your caption text is kept; the photo edge and any choices about an existing caption start over.')
    } catch (e: any) {
      this.toast('error', `Couldn't reload the photo: ${e.message}`)
    }
  }

  /** A new version of the file: the text stays, everything tied to the old pixels (photo edge,
   *  erase masks, existing-caption choices) is reset and the existing-caption check runs again,
   *  so a save never applies the old geometry to the new file. */
  private async applyMeta(s: PhotoSession, meta: PhotoMeta) {
    s.meta = { ...meta, draft: null } as PhotoMeta
    s.proxyVersion++
    s.error = ''
    if (s.meta.info.save_blocked) this.setStatus(s.path, 'error', s.meta.info.save_blocked)
    s.existingGen++ // an earlier check (of the old file) no longer counts
    s.existing = null
    s.existingLoading = true
    s.existingIgnored = false
    s.preExisting = null
    s.draft = { ...s.draft, ...BAND_GEOMETRY }
    if (s.draft.meta?.faces) {
      // face edits follow their faces into this version of the file, where they can be found
      const r = rematchFaces(s.draft.meta.faces, s.meta)
      const m = { ...s.draft.meta }
      if (Object.keys(r.faces).length) m.faces = r.faces
      else delete m.faces
      s.draft = { ...s.draft, meta: m }
      if (r.dropped) this.toast('warn', `${r.dropped} face edit${r.dropped > 1 ? 's were' : ' was'} dropped: the faces changed in the file.`, undefined, 8000, s.meta.name)
      // undo and redo steps name faces by the same keys: move them onto this version's faces too
      const meta = s.meta
      const fix = (snap: string) => {
        try {
          const d = JSON.parse(snap) as PhotoDraft
          if (!d.meta?.faces) return snap
          const f = rematchFaces(d.meta.faces, meta).faces
          d.meta = { ...d.meta, faces: f }
          if (!Object.keys(f).length) delete d.meta.faces
          return JSON.stringify(d)
        } catch {
          return snap
        }
      }
      s.undoStack = s.undoStack.map(fix)
      s.redoStack = s.redoStack.map(fix)
    }
    this.rebaseHistory(s, BAND_GEOMETRY)
    s.lastSnap = this.snap(s)
    s.existingPromise = (async () => {
      try {
        await this.resolveAll(s)
        await this.relayout(s)
      } finally {
        await this.loadExisting(s)
      }
    })()
    // the stored draft now describes this version of the file
    if (s.dirty) this.scheduleDraft(s)
    await s.existingPromise
  }

  prefetchNeighbors() {
    const ps = [this.current + 1, this.current - 1, this.current + 2].filter((k) => k >= 0 && k < this.photos.length).map((k) => this.photos[k].path)
    if (ps.length) post('/api/photo/prefetch', { paths: ps }).catch(() => {})
  }

  /** offerStale: false when the caller carries the newest text over itself (after an overwrite),
   *  so an earlier autosave of that same text is not offered again as "unsaved edits". */
  loadSession(s: PhotoSession, opts: { offerStale?: boolean } = {}): Promise<void> {
    const p = this.loadSessionNow(s, opts)
    s.loadPromise = p
    return p
  }

  private async loadSessionNow(s: PhotoSession, opts: { offerStale?: boolean } = {}) {
    s.error = ''
    // saving waits for the existing-caption check, which starts once the photo is open
    s.existingLoading = true
    try {
      const meta = await get<PhotoMeta>('/api/photo/meta', { path: s.path })
      s.meta = meta
      const tid = this.defaultTemplateId()
      const base: PhotoDraft = { templateId: tid, overrides: {}, blocks: {}, ...BAND_GEOMETRY }
      if (meta.draft) {
        // a draft made for another version of this file (e.g. edits typed while it was
        // overwritten) keeps only its text; the photo edge and mode come from this version's
        // existing-caption check
        const r = draftForFile(meta.draft, meta.stat, base, (id) => !!this.template(id), meta)
        s.draft = r.draft
        s.dirty = true
        this.setStatus(s.path, 'draft')
        if (r.droppedFaces) this.toast('warn', `${r.droppedFaces} face edit${r.droppedFaces > 1 ? 's were' : ' was'} dropped: the faces changed in the file since you edited them.`, undefined, 8000, meta.name)
      } else {
        s.draft = base
        if (opts.offerStale !== false) this.offerStaleDraft(s)
      }
      await this.resolveAll(s)
      s.lastSnap = this.snap(s)
      if (!s.dirty) s.cleanSnap = s.lastSnap
      await this.relayout(s)
      // existing caption detection runs in the background (OCR can take a moment)
      s.existingPromise = this.loadExisting(s)
    } catch (e: any) {
      s.error = e.message
      s.existingLoading = false
      this.setStatus(s.path, 'error', e.message)
    }
  }

  /** Waits until the photo is open and its existing-caption check has finished (saving needs both). */
  async existingReady(s: PhotoSession): Promise<void> {
    for (let i = 0; i < 10; i++) {
      if (s.loadPromise) await s.loadPromise
      const p = s.existingPromise
      if (!s.existingLoading || !p) return
      await p
    }
  }

  /** Run the existing-caption check again (after it failed). */
  recheckExisting(s: PhotoSession) {
    s.existingPromise = this.loadExisting(s)
    return s.existingPromise
  }

  /** A draft saved for an earlier version of this file (the file changed since): offer to use it. */
  async offerStaleDraft(s: PhotoSession) {
    try {
      const r = await get<{ state: PhotoDraft | null; valid: boolean }>('/api/drafts/get', { path: s.path })
      if (!r?.state || r.valid) return
      this.toast('info', 'There are unsaved edits from an earlier version of this photo.', {
        label: 'Use them',
        run: async () => {
          // only the text and style: the photo edge and mode belong to the old version of the file
          const old = draftForFile(r.state, null, s.draft, (id) => !!this.template(id), s.meta)
          s.draft = {
            ...s.draft,
            templateId: old.draft.templateId,
            overrides: old.draft.overrides,
            blocks: old.draft.blocks,
            meta: old.draft.meta,
            faceRows: old.draft.faceRows,
          }
          await this.resolveAll(s)
          this.commit(s)
        },
      }, 0)
    } catch (e) {
      /* optional */
      console.debug('offerStaleDraft failed', e)
    }
  }

  async loadExisting(s: PhotoSession) {
    const gen = ++s.existingGen
    s.existingLoading = true
    try {
      const ex = await post<ExistingAnalysis>('/api/photo/existing', { path: s.path, ocr: true })
      if (gen !== s.existingGen || s.retired) return
      s.existing = ex
      await this.applyExistingDefault(s, ex)
    } catch (e: any) {
      if (gen !== s.existingGen) return
      s.existing = { case: null, source: null, warnings: [`Couldn't check this photo for an existing caption: ${e.message}`] }
    } finally {
      if (gen === s.existingGen) s.existingLoading = false
    }
    // a restored "Erase in place" draft can only be laid out once the band is known
    await this.relayout(s)
  }

  /** Case A (a Photoband band): edit the saved caption instead of adding a second band, unless the
   *  user chose "Add a new band". Applies even when text was typed before the check finished
   *  (that text is kept). Other cases wait for the user's choice in the banner. */
  async applyExistingDefault(s: PhotoSession, ex: ExistingAnalysis) {
    if (ex.case === 'A' && ex.sourceRect && s.draft.mode === 'band' && !s.draft.keepBand) {
      await this.editExisting(s, true)
    }
  }

  /** Case A: edit the caption saved in the file. `auto`: the default on open, which becomes the
   *  starting point (no undo step back into "add a second band"). */
  async editExisting(s: PhotoSession, auto = false) {
    const ex = s.existing
    if (!ex || ex.case !== 'A' || !ex.sourceRect) return
    const st = ex.state
    let tid = st?.templateId && this.template(st.templateId) && !this.template(st.templateId)!.fromFile ? st.templateId : s.draft.templateId
    // the record carries the full template; one that isn't installed is used as a file template
    if (st?.template) tid = this.adoptFileTemplate(st.template)
    const recBlocks: Record<string, BlockState> = {}
    for (const b of st?.blocks || []) recBlocks[b.id] = { id: b.id, custom: !!b.custom, text: b.text }
    let overrides: Overrides = st?.overrides || {}
    let blocks = recBlocks
    if (auto && s.dirty) {
      // typed before the check finished (or a draft made for an earlier version): keep it
      const typed = Object.fromEntries(Object.entries(s.draft.blocks || {}).filter(([, b]) => b.custom))
      blocks = { ...recBlocks, ...typed }
      if (this.hasOverrides(s)) overrides = s.draft.overrides
    }
    const eraseRecord = ex.record?.mode === 'erase' && !!ex.band
    const geom: Geometry = {
      mode: eraseRecord ? 'erase' : 'rebuild',
      sourceRect: eraseRecord ? null : ex.sourceRect,
      photoRect: eraseRecord ? ex.sourceRect : null,
      existingChoice: null,
      brushAdd: null,
      brushRemove: null,
      keepBand: false,
    }
    const tplChanged = tid !== s.draft.templateId
    // edited details stay; the row count is the one saved with the caption unless one was chosen here
    const faceRows = 'faceRows' in s.draft ? s.draft.faceRows ?? null : st?.faceRows ?? null
    const rowsChanged = (faceRows ?? null) !== (s.draft.faceRows ?? null)
    s.draft = { templateId: tid!, overrides, blocks, ...geom, ...keepDetails(s.draft), faceRows }
    if (auto) {
      // the base state: earlier history steps get this geometry too, so undo never goes back
      // to treating the whole captioned image as the photo
      if (s.dirty) this.rebaseHistory(s, geom)
      else {
        s.undoStack = []
        s.redoStack = []
      }
      clearTimeout(s.snapTimer)
      s.snapTimer = null
      s.lastSnap = this.snap(s)
      if (!s.dirty) s.cleanSnap = s.lastSnap
      if (tplChanged || rowsChanged) await this.resolveAll(s)
      this.relayout(s)
      if (s.dirty) this.scheduleDraft(s)
    } else {
      if (tplChanged || rowsChanged) await this.resolveAll(s)
      this.commit(s)
    }
  }

  /** Give every undo/redo step this geometry (the file or its analysis changed under them). */
  private rebaseHistory(s: PhotoSession, geom: Geometry) {
    const fix = (snap: string) => {
      try {
        return JSON.stringify({ ...JSON.parse(snap), ...geom })
      } catch {
        return snap
      }
    }
    s.undoStack = s.undoStack.map(fix)
    s.redoStack = s.redoStack.map(fix)
  }

  /** Case A: keep the old Photoband band as part of the photo and add a new band around it. */
  keepExistingBand(s: PhotoSession) {
    s.draft = { ...s.draft, ...BAND_GEOMETRY, keepBand: true }
    this.commit(s)
  }

  /** Case B/C: start from the recognized text or from the template. */
  useExisting(s: PhotoSession, choice: 'recognized' | 'template', mode: 'rebuild' | 'erase') {
    const ex = s.existing
    if (!ex?.band) return
    const t = this.template(s.draft.templateId)!
    const blocks: Record<string, BlockState> = {}
    if (choice === 'recognized' && ex.blocks?.length) {
      const texts = ex.blocks.filter((b) => (b as any).role !== 'other').map((b) => b.lines.map((l) => escapeMk(l.text)).join('\n'))
      const n = t.blocks.length
      t.blocks.forEach((b, i) => {
        let text = ''
        if (i < n - 1) text = texts[i] ?? ''
        else text = texts.slice(i).join('\n')
        blocks[b.id] = { id: b.id, custom: true, text }
      })
    }
    // Cancel goes back to exactly this (so recognized text can't stay behind as custom blocks)
    if (s.draft.mode === 'band' && !s.draft.existingChoice) s.preExisting = this.snap(s)
    const pr = (s.draft.photoRect ?? ex.band.photo_rect) as Rect
    s.draft = {
      ...s.draft,
      blocks,
      mode,
      sourceRect: mode === 'rebuild' ? pr : null,
      photoRect: pr,
      existingChoice: choice,
    }
    if (mode === 'erase' && ex.style) {
      // prefill the style from the estimate as a starting point
      // sizes are in the template's units: % of the PHOTO width (relative) or pt at the file DPI (physical);
      // a point size is a height, so it uses the vertical DPI
      const pw = pr[2] || s.meta!.info.upright_width
      const dpi = s.meta!.info.dpi?.[1] ?? s.meta!.info.dpi?.[0] ?? null
      const eff = this.effective(s)!
      const physical = eff.scaleMode === 'physical' && !!dpi && dpi > 72.5
      const toSize = (px: number) => (physical ? (px / dpi!) * 72 : (px / pw) * 100)
      const styles = ((ex as any).styles || []) as ({ font_size_px: number; color_hex: string; align: string } | null)[]
      const caps = (ex.blocks || []).map((b, i) => ({ b, st: styles[i] })).filter((x) => (x.b as any).role !== 'other')
      const blocksOv: Record<string, any> = {}
      t.blocks.forEach((b, i) => {
        const st = caps[i]?.st || ex.style!
        blocksOv[b.id] = {
          size: +toSize(st.font_size_px).toFixed(3),
          color: st.color_hex,
          align: st.align === 'center' ? 'center' : st.align === 'right' ? 'right' : 'left',
        }
      })
      s.draft.overrides = { ...s.draft.overrides, blocks: { ...(s.draft.overrides.blocks || {}), ...blocksOv } }
    }
    this.commit(s)
  }

  /** Back to adding a new band (case A: "Add a new band"; case B/C: Cancel). */
  leaveExisting(s: PhotoSession) {
    // case A: an explicit choice to keep the old Photoband band as part of the photo
    const keepBand = s.existing?.case === 'A'
    if (s.preExisting) {
      const prev = JSON.parse(s.preExisting) as PhotoDraft
      s.preExisting = null
      // back to the caption as it was; details edited since then stay
      s.draft = { ...prev, ...BAND_GEOMETRY, keepBand, ...keepDetails(s.draft) }
    } else {
      s.draft = { ...s.draft, ...BAND_GEOMETRY, keepBand }
    }
    this.commit(s)
  }

  setPhotoEdge(s: PhotoSession, rect: Rect) {
    s.draft = { ...s.draft, photoRect: rect, sourceRect: s.draft.mode === 'rebuild' ? rect : s.draft.sourceRect }
    this.commit(s)
  }

  // ------------------------------------------------------------------ text & templates
  /** Resolve the template's formats for this photo. False when a newer resolve started meanwhile
   *  (its reply wins, so a slow earlier reply never replaces the current template's text). */
  async resolveAll(s: PhotoSession): Promise<boolean> {
    const t = this.template(s.draft.templateId)
    if (!t || !s.meta) return false
    const gen = ++s.resolveGen
    const key = this.resolveKey(s)
    const formats: Record<string, string> = {}
    for (const b of t.blocks) formats[b.id] = b.format
    const res = await post('/api/resolve', { ...this.fieldsBody(s), formats, templateName: (t.fromFile?.name ?? t.name) })
    if (gen !== s.resolveGen) return false
    s.resolved = res
    s.resolvedFor = key
    return true
  }

  /** What /api/resolve needs to fill in this photo's fields as they will be saved: the file's
   *  fields, the edited details (applied by the server, as a save writes them) and the row count. */
  fieldsBody(s: PhotoSession): { fields: Record<string, any> | undefined; edits?: MetaEdits; faceRows?: number | null } {
    return { fields: s.meta?.fields, edits: hasEdits(s.draft.meta) ? s.draft.meta : undefined, faceRows: s.draft.faceRows ?? null }
  }

  private resolveKey(s: PhotoSession): string {
    return `${s.draft.templateId}|${s.meta?.stat?.join(':') ?? ''}|${stateHash(JSON.stringify([s.draft.meta ?? null, s.draft.faceRows ?? null]))}`
  }

  /** Re-resolve after edited details changed (debounced: one request per pause in typing). */
  private scheduleResolve(s: PhotoSession) {
    clearTimeout(s.resolveTimer)
    s.resolveTimer = setTimeout(async () => {
      try {
        if (await this.resolveAll(s)) this.relayout(s)
      } catch (e: any) {
        this.resolveFailed(e)
      }
    }, 120)
  }

  private resolveFailedMsg = ''
  private resolveFailed(e: any) {
    const msg = `The caption couldn’t be updated: ${e instanceof ApiError ? e.message : e?.message || e}`
    if (msg !== this.resolveFailedMsg) this.toast('error', msg)
    this.resolveFailedMsg = msg
    setTimeout(() => { if (this.resolveFailedMsg === msg) this.resolveFailedMsg = '' }, 5000)
  }

  /** Waits until `resolved` belongs to the current template (saving needs the right text). */
  async resolvedReady(s: PhotoSession) {
    for (let i = 0; i < 5 && s.resolvedFor !== this.resolveKey(s); i++) {
      if (!this.template(s.draft.templateId) || !s.meta) return
      await this.resolveAll(s)
    }
    // never save caption text made from older details than the ones being saved
    if (this.template(s.draft.templateId) && s.meta && s.resolvedFor !== this.resolveKey(s))
      throw new Error('The caption is still being updated. Try again in a moment.')
  }

  blockText(s: PhotoSession, id: string): string {
    const b = s.draft.blocks[id]
    if (b?.custom) return b.text
    return s.resolved[id]?.text ?? ''
  }

  isCustom(s: PhotoSession, id: string) {
    return !!s.draft.blocks[id]?.custom
  }

  setBlockText(s: PhotoSession, id: string, markup: string) {
    const linked = s.resolved[id]?.text ?? ''
    if (!s.draft.blocks[id]?.custom && markup === linked) return
    s.draft.blocks = { ...s.draft.blocks, [id]: { id, custom: true, text: markup } }
    this.commit(s)
  }

  resetBlock(s: PhotoSession, id: string) {
    const { [id]: _, ...rest } = s.draft.blocks
    s.draft.blocks = rest
    this.commit(s)
  }

  customBlocksOutdated(s: PhotoSession): string[] {
    const t = this.template(s.draft.templateId)
    if (!t) return []
    return t.blocks.filter((b) => s.draft.blocks[b.id]?.custom).map((b) => b.id)
  }

  async setTemplate(s: PhotoSession, id: string) {
    if (!this.template(id)) return
    const hadCustom = Object.values(s.draft.blocks).some((b) => b.custom)
    s.draft = { ...s.draft, templateId: id, overrides: {} }
    await this.resolveAll(s)
    // another template was picked while this one resolved: that later choice is the one that counts
    if (s.draft.templateId !== id) return
    this.commit(s)
    this.saveSettings({ session: { lastTemplate: id } }).catch(() => {})
    if (hadCustom) this.toast('info', 'Caption lines you edited kept their text.', { label: 'Reset them to the template', run: () => this.resetAllBlocks(s) }, 9000)
  }

  resetAllBlocks(s: PhotoSession) {
    s.draft.blocks = {}
    this.commit(s)
  }

  /** Discard this photo's text and style edits (blocks and overrides); existing-caption choices stay. */
  revertToTemplate(s: PhotoSession) {
    s.draft = { ...s.draft, blocks: {}, overrides: {} }
    this.commit(s)
  }

  hasEdits(s: PhotoSession | null): boolean {
    return !!s && (Object.keys(s.draft.blocks || {}).length > 0 || this.hasOverrides(s))
  }

  /** Template edited in Settings: re-resolve every open photo; custom blocks stay. */
  async templatesChanged() {
    await this.reloadTemplates()
    const switched: string[] = []
    for (const s of this.sessions.values()) {
      if (!this.template(s.draft.templateId)) {
        s.draft.templateId = this.defaultTemplateId()
        if (s.meta && !s.retired) {
          switched.push(s.meta.name)
          this.commit(s)
        }
      }
      if (s.meta) await this.resolveAll(s)
    }
    if (this.session) await this.relayout(this.session)
    if (switched.length) {
      const def = this.template(this.defaultTemplateId())?.name ?? 'the default template'
      const who = switched.length === 1 ? switched[0] : `${switched.length} open photos`
      this.toast('warn', `The template used by ${who} no longer exists, so “${def}” is used instead.`, undefined, 9000)
    }
  }

  setStyle(s: PhotoSession, blockId: string, patch: Record<string, any>) {
    const ov = clone(s.draft.overrides || {})
    ov.blocks = ov.blocks || {}
    ov.blocks[blockId] = { ...(ov.blocks[blockId] || {}), ...patch }
    s.draft.overrides = ov
    this.commit(s)
  }

  setLayout(s: PhotoSession, patch: Record<string, any>) {
    const ov = clone(s.draft.overrides || {})
    ov.layout = deepAssign(ov.layout || {}, clone(patch))
    s.draft.overrides = ov
    this.commit(s)
  }

  setScaleMode(s: PhotoSession, mode: 'relative' | 'physical') {
    s.draft.overrides = { ...clone(s.draft.overrides || {}), scaleMode: mode }
    this.commit(s)
  }

  clearOverride(s: PhotoSession, kind: 'layout' | 'block', key: string, blockId?: string) {
    const ov = clone(s.draft.overrides || {})
    if (kind === 'block' && blockId && ov.blocks?.[blockId]) delete (ov.blocks[blockId] as any)[key]
    if (kind === 'layout' && ov.layout) {
      const parts = key.split('.')
      let o: any = ov.layout
      for (let i = 0; i < parts.length - 1; i++) o = o?.[parts[i]]
      if (o) delete o[parts[parts.length - 1]]
    }
    s.draft.overrides = ov
    this.commit(s)
  }

  hasOverrides(s: PhotoSession): boolean {
    const o = s.draft.overrides || {}
    return !!(o.scaleMode || (o.layout && Object.keys(o.layout).length) || (o.blocks && Object.values(o.blocks).some((b) => Object.keys(b || {}).length)))
  }

  effective(s: PhotoSession): Template | null {
    const t = this.template(s.draft.templateId)
    if (!t) return null
    return effectiveTemplate(t, s.draft.overrides)
  }

  async saveOverridesAsTemplate(s: PhotoSession, asNew: boolean, name?: string) {
    const eff = this.effective(s)
    if (!eff) return
    const base = this.template(s.draft.templateId)!
    const baseName = base.fromFile?.name ?? base.name
    const { fromFile: _f, ...effClean } = eff
    const t = { ...effClean, name: name || (asNew ? baseName + ' copy' : baseName) }
    if (base.fromFile) {
      // a template that came with the photo is saved as a new installed template
      asNew = true
    } else if (!asNew && base.builtin) {
      this.toast('warn', 'Built-in templates are read-only; saved as a new template instead.')
      asNew = true
    }
    const saved = await post<Template>('/api/templates', { template: asNew ? { ...t, id: undefined } : t, new: asNew })
    await this.reloadTemplates()
    s.draft = { ...s.draft, templateId: saved.id, overrides: {} }
    await this.resolveAll(s)
    this.commit(s)
    this.toast('success', asNew ? `Saved as template “${saved.name}”.` : `Updated template “${saved.name}”.`)
  }

  // ------------------------------------------------------------------ change tracking / undo / drafts
  snap(s: PhotoSession) {
    return JSON.stringify(s.draft)
  }

  /** Record a change. `boundary`: a separate undo step even right after another change (a face
   *  dropped, a name entered), instead of merging it with the keystrokes before it. */
  commit(s: PhotoSession, markDirty = true, boundary = false) {
    if (markDirty) {
      s.dirty = true
      this.setStatus(s.path, 'draft')
    }
    if (boundary) {
      clearTimeout(s.snapTimer)
      s.snapTimer = null
    }
    // edited details change the fields the caption is made from
    if (s.resolvedFor && s.resolvedFor !== this.resolveKey(s)) this.scheduleResolve(s)
    // coalesce keystrokes into one undo step
    const cur = this.snap(s)
    if (cur !== s.lastSnap) {
      if (!s.snapTimer) {
        s.undoStack.push(s.lastSnap)
        if (s.undoStack.length > 200) s.undoStack.shift()
        s.redoStack = []
      }
      clearTimeout(s.snapTimer)
      s.snapTimer = setTimeout(() => (s.snapTimer = null), 600)
      s.lastSnap = cur
    }
    this.relayout(s)
    this.scheduleDraft(s)
  }

  undo(s: PhotoSession) {
    const prev = s.undoStack.pop()
    if (prev === undefined) return
    s.redoStack.push(this.snap(s))
    s.draft = JSON.parse(prev)
    s.lastSnap = prev
    clearTimeout(s.snapTimer)
    s.snapTimer = null
    this.afterHistory(s)
  }

  redo(s: PhotoSession) {
    const next = s.redoStack.pop()
    if (next === undefined) return
    s.undoStack.push(this.snap(s))
    s.draft = JSON.parse(next)
    s.lastSnap = next
    this.afterHistory(s)
  }

  private async afterHistory(s: PhotoSession) {
    s.dirty = true
    this.setStatus(s.path, 'draft')
    this.scheduleDraft(s)
    try {
      await this.resolveAll(s)
    } catch (e: any) {
      this.resolveFailed(e)
    }
    this.relayout(s)
  }

  scheduleDraft(s: PhotoSession) {
    clearTimeout(s.draftTimer)
    // batch review edits belong to the batch (saved with Save all), never to a single-photo draft
    if (this.batchReview || (s as any).__batch || s.retired) return
    // debounce keystrokes, but never wait more than 3 s while someone keeps typing
    const now = Date.now()
    if (!s.draftPendingSince) s.draftPendingSince = now
    const wait = Math.max(0, Math.min(800, 3000 - (now - s.draftPendingSince)))
    s.draftTimer = setTimeout(() => this.flushDraft(s), wait)
  }

  /** Write the draft now. It records which version of the file it was made for (`_stat`), so a
   *  draft that outlives that version only gives back its text, never its photo edge. */
  flushDraft(s: PhotoSession): Promise<void> {
    clearTimeout(s.draftTimer)
    s.draftTimer = null
    s.draftPendingSince = 0
    if (!s.dirty || !s.meta || s.retired || this.batchReview || (s as any).__batch) return Promise.resolve()
    const snap = this.snap(s)
    // _size: face edits of a stale draft are only re-used on a photo of the same shape
    const state = { ...JSON.parse(snap), _stat: s.meta.stat, _size: [s.meta.info.upright_width, s.meta.info.upright_height] }
    return post('/api/drafts', { path: s.path, state, hash: stateHash(snap) }, { keepalive: true })
      .then(() => {})
      .catch((e) => console.debug('draft autosave failed', e))
  }

  /** A draft holding only the edited details (after a copy was saved: the caption is done, the
   *  original's details are not). */
  keepDetailsDraft(s: PhotoSession) {
    if (!s.meta || s.retired || !hasEdits(s.draft.meta)) return
    const d = JSON.parse(this.snap(s)) as PhotoDraft
    const state = { templateId: d.templateId, overrides: {}, blocks: {}, ...BAND_GEOMETRY, meta: d.meta, faceRows: d.faceRows ?? null,
      _stat: s.meta.stat, _size: [s.meta.info.upright_width, s.meta.info.upright_height] }
    // the hash a session that opens this draft computes for it (an overwrite of the original with
    // these details removes it in any case: the server deletes a details-only draft it wrote)
    const { _stat, _size, ...plain } = state
    void _stat
    void _size
    post('/api/drafts', { path: s.path, state, hash: stateHash(JSON.stringify(plain)) }, { keepalive: true }).catch(() => {})
  }

  /** Write every pending draft (closing the window, Close all). */
  flushAll(): Promise<void> {
    return Promise.all([...this.sessions.values()].map((s) => (s.draftTimer || s === this.session ? this.flushDraft(s) : null))).then(() => {})
  }

  setStatus(path: string, status: Status, error?: string) {
    const i = this.photos.findIndex((p) => p.path === path)
    if (i >= 0 && (this.photos[i].status !== status || this.photos[i].error !== error)) {
      this.photos[i] = { ...this.photos[i], status, error }
    }
  }

  // ------------------------------------------------------------------ layout
  layoutInput(s: PhotoSession) {
    const eff = this.effective(s)
    if (!eff || !s.meta) return null
    const info = s.meta.info
    const W = info.upright_width
    const H = info.upright_height
    const texts: Record<string, string> = {}
    for (const b of eff.blocks) texts[b.id] = this.blockText(s, b.id)
    let mode: 'band' | 'erase' = 'band'
    let sourceRect: Rect = [0, 0, W, H]
    let erase
    if (s.draft.mode === 'rebuild' && s.draft.sourceRect) sourceRect = s.draft.sourceRect
    if (s.draft.mode === 'erase' && s.existing?.band) {
      mode = 'erase'
      const pr = (s.draft.photoRect ?? s.existing.band.photo_rect) as Rect
      const bandRect = eraseBandRect(pr, W, H)
      erase = { canvas: [W, H] as [number, number], photoRect: pr, bandRect, bandColor: s.existing.band.band_color_hex || '#ffffff' }
      sourceRect = pr
    }
    // info.dpi is upright [horizontal, vertical]: widths use the first, heights and point sizes the second
    const dpi = info.dpi ? info.dpi[0] : null
    const dpiY = info.dpi ? info.dpi[1] ?? info.dpi[0] : null
    return { template: eff, sourceRect, dpi, dpiY, texts, mode, erase, gray: info.channels <= 2 }
  }

  /** Lay out the draft as it is now (the input is captured before any await). */
  private async layoutNow(s: PhotoSession): Promise<LayoutResult | null> {
    const inp = this.layoutInput(s)
    if (!inp) return null
    const fontIds = inp.template.blocks.map((b) => resolveFont(b.style.font).id)
    await ensureFonts(fontIds)
    return computeLayout(inp as any)
  }

  async relayout(s: PhotoSession) {
    const gen = ++s.layoutGen
    try {
      const lay = await this.layoutNow(s)
      // a newer relayout started while fonts loaded: its result wins
      if (lay && gen === s.layoutGen) {
        s.layout = lay
        if (s.error.startsWith('Layout failed')) s.error = ''
      }
    } catch (e: any) {
      console.error(e)
      if (gen === s.layoutGen) s.error = 'Layout failed: ' + e.message
    }
  }

  fontsLoaded() {
    clearMeasureCache()
    this.fontsVersion++
    if (this.session) this.relayout(this.session)
  }

  private fontsTimer: any = null
  private watchFonts() {
    // fonts that finish loading later change the metrics: re-measure and lay out again
    if ('onFontsChanged' in fontsModule) {
      ;(fontsModule as any).onFontsChanged(() => {
        clearTimeout(this.fontsTimer)
        this.fontsTimer = setTimeout(() => this.fontsLoaded(), 30)
      })
    }
  }

  // ------------------------------------------------------------------ photo details & faces
  /** The faces as they will be saved (the file's, with this photo's face edits). */
  faces(s: PhotoSession): { named: Face[]; unnamed: Face[] } {
    return effectiveFaces(s.meta, s.draft.meta)
  }

  /** The file's own value of a text detail. */
  fileDetail(s: PhotoSession, key: TextField): string {
    return String(s.meta?.fields?.[key] ?? '')
  }

  /** The keywords the file has, without photokin's date marker (the date editor owns that one). */
  fileKeywords(s: PhotoSession): string[] {
    return ((s.meta?.fields?.keywords as string[]) || []).filter((k) => !isDateMarker(k))
  }

  private setMeta(s: PhotoSession, patch: Partial<MetaEdits>, drop: (keyof MetaEdits)[] = [], boundary = false) {
    const m: MetaEdits = { ...(s.draft.meta || {}), ...patch }
    for (const k of drop) delete m[k]
    s.draft = { ...s.draft, meta: Object.keys(m).length ? m : undefined }
    this.commit(s, true, boundary)
  }

  /** Edit a text detail; typing the file's own value back is no edit. (Compared exactly, not trimmed:
   *  a space typed between two words must not be taken back while typing.) */
  setDetail(s: PhotoSession, key: TextField, value: string) {
    value = cleanText(value, key === 'caption' || key === 'notes')
    if (value === this.fileDetail(s, key)) this.setMeta(s, {}, [key])
    else this.setMeta(s, { [key]: value })
  }

  resetDetail(s: PhotoSession, key: keyof MetaEdits) {
    this.setMeta(s, {}, [key], true)
  }

  setKeywords(s: PhotoSession, list: string[]) {
    const clean = addKeywords([], list)
    const file = this.fileKeywords(s)
    const same = clean.length === file.length && clean.every((k, i) => k === file[i])
    if (same) this.setMeta(s, {}, ['keywords'], true)
    else this.setMeta(s, { keywords: clean }, [], true)
  }

  /** The keywords as they will be saved (edited or the file's), without the date marker. */
  keywords(s: PhotoSession): string[] {
    return s.draft.meta?.keywords ?? this.fileKeywords(s)
  }

  /** undefined: back to the file's date; null: no date. */
  setDate(s: PhotoSession, d: DateEdit | null | undefined) {
    if (d === undefined) this.setMeta(s, {}, ['date'], true)
    else this.setMeta(s, { date: d }, [], true)
  }

  discardDetails(s: PhotoSession) {
    s.draft = { ...s.draft, meta: undefined }
    this.commit(s, true, true)
  }

  setFaceRows(s: PhotoSession, n: number | null) {
    s.draft = { ...s.draft, faceRows: n }
    this.commit(s, true, true)
  }

  private faceEdits(s: PhotoSession): Record<string, FaceEdit> {
    return { ...(s.draft.meta?.faces || {}) }
  }

  private putFaces(s: PhotoSession, faces: Record<string, FaceEdit>, boundary: boolean, keywords?: string[] | null) {
    const patch: Partial<MetaEdits> = {}
    const drop: (keyof MetaEdits)[] = []
    if (Object.keys(faces).length) patch.faces = faces
    else drop.push('faces')
    if (keywords) {
      // the file's own keywords again (a name renamed back): no edit, in the file's order
      const file = this.fileKeywords(s)
      const same = keywords.length === file.length && keywords.every((k) => file.includes(k))
      if (same) drop.push('keywords')
      else patch.keywords = keywords
    }
    this.setMeta(s, patch, drop, boundary)
  }

  /** Change a face's name and/or box. A rename is reflected in the keywords when the file lists its
   *  people as keywords (Lightroom does), so the two stay in step. */
  updateFace(s: PhotoSession, key: string, patch: { name?: string; box?: Box | null }, boundary = true) {
    const base = [...(s.meta?.faces.named || []), ...(s.meta?.faces.unnamed || [])].find((f) => f.key === key)
    const before = this.faces(s)
    const cur = [...before.named, ...before.unnamed].find((f) => f.key === key)
    if (!cur) return
    const faces = this.faceEdits(s)
    const e: FaceEdit = { ...(faces[key] || (base ? { was: { name: base.name, box: base.box as Box | null } } : {})) }
    if (patch.name !== undefined) e.name = patch.name.trim()
    if (patch.box !== undefined) e.box = patch.box
    if (base) {
      // back to the file's value: no edit
      if (e.name !== undefined && e.name === base.name) delete e.name
      if (e.box !== undefined && JSON.stringify(e.box) === JSON.stringify(base.box)) delete e.box
    }
    if (base && e.name === undefined && e.box === undefined && !e.deleted) delete faces[key]
    else faces[key] = e
    let kws: string[] | null = null
    if (patch.name !== undefined && patch.name.trim() !== cur.name) {
      if (patch.name.trim()) rememberName(patch.name)
      const after = [...before.named.filter((f) => f.key !== key).map((f) => f.name), patch.name.trim()].filter(Boolean)
      kws = peopleKeywords(this.keywords(s), this.peopleAsKeywords(s), after, cur.name, patch.name.trim(), this.removableName(s, cur.name, base))
    }
    this.putFaces(s, faces, boundary, kws)
  }

  /** Does the file list its tagged people as keywords too (as Lightroom does)? Decided from the
   *  file's own faces and keywords, never from edits. */
  private peopleAsKeywords(s: PhotoSession): boolean {
    const kws = new Set(this.fileKeywords(s).map((k) => k.toLowerCase()))
    return (s.meta?.faces?.named || []).some((f) => f.name && kws.has(f.name.toLowerCase()))
  }

  /** May a name's keyword go when the name leaves the faces? Only when it was there because of
   *  this face (the file's name for it) or was added by these edits, never a keyword of its own. */
  private removableName(s: PhotoSession, name: string, base: Face | undefined): boolean {
    const own = this.fileKeywords(s).some((k) => k.toLowerCase() === name.toLowerCase())
    return !own || (!!base && base.name.toLowerCase() === name.toLowerCase())
  }

  deleteFace(s: PhotoSession, key: string) {
    const before = this.faces(s)
    const cur = [...before.named, ...before.unnamed].find((f) => f.key === key)
    if (!cur) return
    const faces = this.faceEdits(s)
    if (key.startsWith('new:')) delete faces[key]
    else {
      const base = [...(s.meta?.faces.named || []), ...(s.meta?.faces.unnamed || [])].find((f) => f.key === key)
      faces[key] = { was: { name: base?.name ?? cur.name, box: (base?.box ?? cur.box) as Box | null }, deleted: true }
    }
    const after = before.named.filter((f) => f.key !== key).map((f) => f.name)
    const fileFace = [...(s.meta?.faces.named || []), ...(s.meta?.faces.unnamed || [])].find((f) => f.key === key)
    const kws = cur.name ? peopleKeywords(this.keywords(s), this.peopleAsKeywords(s), after, cur.name, '', this.removableName(s, cur.name, fileFace)) : null
    if (this.selectedFace === key) this.selectedFace = null
    this.putFaces(s, faces, true, kws)
  }

  /** A new face box (normalized, upright), optionally with a name; returns its key. */
  addFace(s: PhotoSession, box: Box, name = ''): string {
    const faces = this.faceEdits(s)
    const key = newFaceKey()
    faces[key] = { name: name.trim(), box }
    let kws: string[] | null = null
    if (name.trim()) {
      rememberName(name)
      const before = this.faces(s)
      const names = before.named.map((f) => f.name)
      kws = peopleKeywords(this.keywords(s), this.peopleAsKeywords(s), [...names, name.trim()], '', name.trim(), false)
    }
    this.putFaces(s, faces, true, kws)
    return key
  }

  /** A detail or face name still being typed is committed (its field loses focus) before a save
   *  takes its snapshot. */
  async commitTyping() {
    if (typeof document === 'undefined') return
    const el = document.activeElement as HTMLElement | null
    if (el && el !== document.body && el.closest?.('.details, .faces-layer') && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) {
      el.blur()
      await sleep(0)
    }
  }

  /** Is the caption part of the draft untouched (only details were edited)? */
  private captionUnedited(s: PhotoSession): boolean {
    const strip = (snap: string) => {
      try {
        const d = JSON.parse(snap)
        delete d.meta
        return JSON.stringify(d)
      } catch {
        return snap
      }
    }
    if (s.cleanSnap) return strip(this.snap(s)) === strip(s.cleanSnap)
    // no clean state to compare with (opened from a draft): only a caption no one has touched in
    // any way counts as unedited (the default template, no text, style, rows or photo-edge choices)
    const d = s.draft
    return d.templateId === this.defaultTemplateId() && !Object.values(d.blocks || {}).some((b) => b.custom) &&
      !this.hasOverrides(s) && !d.faceRows && (d.mode ?? 'band') === 'band' && !d.sourceRect && !d.photoRect &&
      !d.existingChoice && !d.keepBand && !d.brushAdd && !d.brushRemove
  }

  /** A detail as `meta` (a version of the file) has it, in the shape an edit of it takes. */
  detailOf(meta: PhotoMeta | null | undefined, k: keyof MetaEdits): unknown {
    const f = meta?.fields || {}
    if (k === 'faces') return undefined
    if (k === 'keywords') return ((f.keywords as string[]) || []).filter((x) => !isDateMarker(x))
    if (k === 'date') {
      const d = dateState(f)
      // a date in words ("Summer 1952") is no edit the date editor can make: undefined, not
      // "no date" (the caller says it could not be put back)
      return d.kind === 'date' ? { iso: d.iso, level: d.level } : d.kind === 'none' ? null : undefined
    }
    return String(f[k] ?? '')
  }

  /** Edits that only say what the file already has are no edits. */
  private withoutNoOps(s: PhotoSession, m: MetaEdits | undefined): MetaEdits | undefined {
    if (!m) return undefined
    const out: MetaEdits = { ...m }
    for (const k of Object.keys(out) as (keyof MetaEdits)[]) {
      if (k !== 'faces' && JSON.stringify(out[k]) === JSON.stringify(this.detailOf(s.meta, k))) delete out[k]
    }
    return Object.keys(out).length ? out : undefined
  }

  /** Write the edited details into the photo itself (nothing else changes). */
  async saveDetails(s: PhotoSession): Promise<boolean> {
    await this.commitTyping()
    const edits = s.draft.meta
    if (!s.meta || !hasEdits(edits)) return false
    if (this.batchReview) {
      this.toast('warn', 'In batch review, edits are saved with Save all.')
      return false
    }
    if (this.saving) return false
    this.saving = true
    clearTimeout(s.draftTimer)
    const sent = JSON.stringify(edits)
    try {
      const res = await post<SaveResult & { meta?: PhotoMeta }>('/api/photo/details', { path: s.path, edits, expected_stat: s.meta.stat })
      if (!res.ok || !res.meta) {
        this.flushDraft(s)
        if (res.code === 'changed')
          this.toast('error', `Details not saved: ${res.error} Your edits are kept.`, { label: 'Reload photo', run: () => this.reloadPhoto(s) })
        else this.toast('error', `Details not saved: ${res.error}`)
        return false
      }
      // the file has them now: the same pixels, so the caption geometry and analysis stay
      const beforeMeta = s.meta
      s.meta = { ...res.meta, draft: null } as PhotoMeta
      // details changed while the file was written stay edits (face edits can't: the file's faces
      // were just renumbered)
      const lost: string[] = []
      const since = editsSince(s.draft.meta, JSON.parse(sent), (k) => {
        const v = this.detailOf(beforeMeta, k)
        if (v === undefined && k !== 'faces') lost.push(k === 'date' ? `the date “${beforeMeta?.fields?.date}”` : k)
        return v
      })
      s.draft = { ...s.draft, meta: this.withoutNoOps(s, since.meta) }
      if (lost.length) this.toast('warn', `Reset while saving, but ${lost.join(', ')} can’t be written back by Photoband; the photo keeps what was saved.`, undefined, 10000, s.meta.name)
      if (since.droppedFaces) this.toast('warn', 'Face changes made while saving were not kept. Check the faces again.', undefined, 8000, s.meta.name)
      const strip = (snap: string) => {
        try {
          const d = JSON.parse(snap)
          delete d.meta
          return JSON.stringify(d)
        } catch {
          return snap
        }
      }
      // undo no longer steps back into edits that are saved now
      s.undoStack = s.undoStack.map(strip)
      s.redoStack = s.redoStack.map(strip)
      s.lastSnap = this.snap(s)
      this.selectedFace = null
      await this.resolveAll(s)
      this.relayout(s)
      if (!hasEdits(s.draft.meta) && this.captionUnedited(s)) {
        // the details were all there was: nothing is left unsaved, and no draft either (one may
        // remain from a saved copy, made for the file as it was)
        s.dirty = false
        this.setStatus(s.path, 'saved')
        post('/api/drafts', { path: s.path, state: null }).catch(() => {})
      } else {
        s.dirty = true
        this.flushDraft(s)
      }
      const extra = res.backup_path ? ' (original backed up first)' : ''
      this.toast('success', `Saved the details into the photo${extra}.`, undefined, 4000, s.meta.name)
      return true
    } catch (e: any) {
      this.flushDraft(s)
      this.toast('error', `Details not saved: ${e instanceof ApiError ? e.message : e?.message || e}`)
      return false
    } finally {
      this.saving = false
    }
  }

  warnings(s: PhotoSession): Warning[] {
    const out: Warning[] = [...(s.layout?.warnings || [])]
    const mw = s.meta?.warnings || []
    if (mw.includes('region dimensions mismatch'))
      out.push({ kind: 'metadata', code: 'names-regions', message: 'The face tags were made for a different size of this image, so the left-to-right order of the names may be off.' })
    // the faces as edited here, not only as the file has them
    if (s.meta) {
      const faces = this.faces(s)
      if (!faces.named.length) {
        const nb = this.effective(s)?.blocks.find((b) => b.format.includes('{names'))
        if (nb) out.push({ kind: 'metadata', code: 'names-missing', message: `No names are tagged on faces in this photo, so the ${nb.name} line is blank. Turn on Faces below the photo to name them.` })
      } else if (faces.named.some((f) => !f.box))
        out.push({ kind: 'metadata', code: 'names-order', message: 'Some names have no place on the photo, so the left-to-right order may be wrong. Mark them in the People list (Metadata tab).' })
    }
    const info = s.meta?.info
    if (info?.save_blocked) out.unshift({ kind: 'info', message: info.save_blocked })
    else if (info && info.pages > 1 && !this.settings.saving.allowMultipageSave) out.unshift({ kind: 'info', message: `This TIFF has ${info.pages} pages. Saving would drop all but the first, so it is blocked (allow it in Settings › Saving).` })
    return out
  }

  // ------------------------------------------------------------------ saving
  canSave(s: PhotoSession | null): string | null {
    if (this.batchReview) return 'In batch review, edits are saved with Save all.'
    if (!s?.meta || !s.layout) return 'Nothing to save yet.'
    const info = s.meta.info
    if (info.save_blocked) return info.save_blocked
    if (info.pages > 1 && !this.settings.saving.allowMultipageSave) return 'This is a multi-page TIFF, and saving keeps only the first page. Allow it in Settings › Saving › Advanced.'
    return this.geometryProblem(s)
  }

  /** Why the draft's photo edge can't be trusted for a save (null when it can). Guards against a
   *  second band stacked onto an existing one, and an "edit the old caption" draft without one. */
  geometryProblem(s: PhotoSession): string | null {
    if (s.existingLoading || !s.existing) return 'Checking for an existing caption…'
    const ex = s.existing
    const d = s.draft
    if (d.mode === 'erase' && !ex.band)
      return 'This photo is set to erase the old caption in place, but the old caption wasn’t found in this version of the file. Check again, or add a new band instead.'
    if (d.mode === 'rebuild' && (!d.sourceRect || !ex.case || ex.case === 'D'))
      return 'This photo is set to replace an old caption, but the old caption wasn’t found in this version of the file. Check again, or add a new band instead.'
    if (ex.case === 'A' && d.mode === 'band' && !d.keepBand)
      return 'This photo already has a Photoband caption. Choose “Edit existing caption” or “Add a new band” above the photo.'
    return null
  }

  /** A scan with a handwritten or printed (physical) caption. The server refuses to overwrite it
   *  in every mode (erase, replace, or a new band that keeps the handwriting) unless Settings ›
   *  Saving allows it, so the UI applies the same rule whatever the draft's mode. */
  isCaseC(s: PhotoSession) {
    return s.existing?.case === 'C'
  }

  /** Why Overwrite original is off for this photo (null when it isn't). */
  overwriteBlocked(s: PhotoSession | null): string | null {
    if (s && this.isCaseC(s) && !this.settings?.saving?.allowOverwriteHandwritten) return CASE_C_OVERWRITE_OFF
    return null
  }

  async save(s: PhotoSession, mode: 'copy' | 'overwrite' | 'copyAs', opts: { destPath?: string; onExists?: string; embedMarker?: boolean } = {}): Promise<SaveResult | null> {
    await this.commitTyping()
    // the existing-caption check decides where the photo is: never save before it has finished
    if (s.existingLoading) await this.existingReady(s)
    const why = this.canSave(s)
    if (why) {
      this.toast('warn', why)
      return null
    }
    if (this.saving) return null
    this.saving = true
    this.savingMode = mode === 'overwrite' ? 'overwrite' : 'copy'
    // a pending autosave of the state being saved would only re-create the draft afterwards
    clearTimeout(s.draftTimer)
    try {
      const { job, tiles, snap } = await this.buildJob(s, mode, opts)
      const form = new FormData()
      form.append('job', JSON.stringify(job))
      tiles.forEach((t, i) => form.append(`tile${i}`, t.blob, `tile${i}.png`))
      const res = await postForm<SaveResult>('/api/save', form)
      if (res.ok) {
        const overwrote = mode === 'overwrite' || (!!res.out_path && res.out_path === res.path)
        // edits typed while the save ran are not in the file: they stay unsaved (and autosaved)
        const newer = this.snap(s) !== snap ? this.snap(s) : null
        if (newer) {
          this.setStatus(s.path, 'draft')
          // after an overwrite the old session's photo edge no longer fits the file: the fresh
          // session below takes over the text instead
          if (!overwrote) this.flushDraft(s)
        } else {
          s.dirty = false
          s.cleanSnap = snap
          this.setStatus(s.path, 'saved')
        }
        const extra = res.backup_path ? ' (backup kept)' : ''
        const show = { label: 'Show in folder', run: () => { post('/api/open-folder', { which: 'file', path: res.out_path }).catch(() => {}) } }
        if (overwrote) this.toast('success', 'Overwrote the original' + extra, show)
        else this.toast('success', 'Saved' + extra, show, 5000, basename(res.out_path))
        const savedMeta = (JSON.parse(snap) as PhotoDraft).meta
        if (!overwrote && hasEdits(savedMeta)) {
          // the copy has the edited details; the original doesn't yet. Keep them for it, so they
          // are not lost when the app closes, and offer to write them now.
          if (!newer) this.keepDetailsDraft(s)   // (newer edits were autosaved whole just above)
          this.toast('info', 'The edited details went into the copy. The original still has its old details.', { label: 'Save to original', run: () => this.saveDetails(s) }, 12000, s.meta?.name)
        }
        if (newer) this.toast('info', 'Changes made while saving are kept as unsaved edits.')
        if (overwrote) {
          // the file changed: reload its state (it is now a case A photo). The old session must
          // never write a draft again: its geometry belongs to the file before the overwrite.
          s.retired = true
          clearTimeout(s.draftTimer)
          s.draftTimer = null
          this.sessions.delete(s.path)
          const fresh = new PhotoSession(s.path, s.draft.templateId)
          this.sessions.set(s.path, fresh)
          if (this.session === s) this.session = fresh
          // the text typed during the save is carried over below: an autosave of it (made for the
          // file before the overwrite) must not be offered again as "unsaved edits"
          await this.loadSession(fresh, { offerStale: !newer })
          if (newer) {
            await this.existingReady(fresh)
            // only the text and style carry over; the photo edge comes from the new file's analysis
            const nd = JSON.parse(newer) as PhotoDraft
            // (details edited during the save are not in the file: they carry over too)
            const lostDate = { v: false }
            const since = editsSince(nd.meta, (JSON.parse(snap) as PhotoDraft).meta, (k) => {
              const v = this.detailOf(s.meta, k)
              if (v === undefined && k === 'date') lostDate.v = true
              return v
            })
            if (lostDate.v) this.toast('warn', `The date was reset while saving, but “${s.meta?.fields?.date}” can’t be written back by Photoband; the photo keeps the date that was saved.`, undefined, 10000)
            if (since.droppedFaces) this.toast('warn', 'Face changes made while saving were not kept. Check the faces again.', undefined, 8000)
            fresh.draft = { ...fresh.draft, templateId: this.template(nd.templateId) ? nd.templateId : fresh.draft.templateId, overrides: nd.overrides, blocks: nd.blocks, faceRows: nd.faceRows ?? null, meta: this.withoutNoOps(fresh, since.meta) }
            await this.resolveAll(fresh)
            this.commit(fresh)
          } else {
            this.setStatus(s.path, 'saved')
          }
        }
      } else if (res.code === 'exists' || res.code === 'source') {
        this.flushDraft(s)
        return res
      } else if (res.code === 'changed') {
        this.flushDraft(s)
        this.setStatus(s.path, s.dirty ? 'draft' : 'error', res.error)
        this.toast('error', `Not saved: ${res.error} Reload it to see the current file; your edits are kept.`, { label: 'Reload photo', run: () => this.reloadPhoto(s) })
      } else {
        this.flushDraft(s)
        this.setStatus(s.path, s.dirty ? 'draft' : 'error', res.error)
        this.toast('error', `Save failed: ${res.error}`)
      }
      return res
    } catch (e: any) {
      this.flushDraft(s)
      this.toast('error', `Save failed: ${e instanceof ApiError ? e.message : e?.message || e}`)
      return null
    } finally {
      this.saving = false
      this.savingMode = null
    }
  }

  /** Everything a save needs: the layout, the rendered text tiles and the editor state for the record.
   *  Throws when the draft's photo edge can't be trusted (see geometryProblem). */
  async buildJob(s: PhotoSession, mode: string, opts: { destPath?: string; onExists?: string; embedMarker?: boolean } = {}) {
    const problem = this.geometryProblem(s)
    if (problem) throw new Error(problem)
    // the caption text must come from the current template, not a resolve still on its way
    await this.resolvedReady(s)
    // Everything is captured from the draft up front: edits typed while fonts load or tiles
    // render must not end up half in the file. `snap` identifies exactly what was saved.
    const snap = this.snap(s)
    const eff = this.effective(s)!
    const blocks = eff.blocks.map((b) => ({ id: b.id, custom: this.isCustom(s, b.id), text: this.blockText(s, b.id) }))
    const d = JSON.parse(snap) as PhotoDraft
    const tpl = this.template(d.templateId)
    const job: any = {
      path: s.path,
      mode,
      dest_path: opts.destPath,
      on_exists: opts.onExists,
      embed_marker: opts.embedMarker,
      state: { templateId: tpl?.fromFile?.id ?? d.templateId, template: this.recordTemplate(tpl), overrides: d.overrides, blocks, faceRows: d.faceRows ?? null },
      // the file's fields; the server applies the edited details (meta_edits) as it writes them
      fields: s.meta!.fields,
      meta_edits: hasEdits(d.meta) ? d.meta : undefined,
      template_name: tpl?.fromFile?.name ?? eff.name,
      case: s.existing?.case && d.mode !== 'band' ? s.existing.case : null,
      expected_stat: s.meta!.stat,
      draft_hash: stateHash(snap),
    }
    if (d.mode === 'erase' && s.existing?.band) {
      job.erase = {
        band: s.existing.band,
        photoRect: d.photoRect ?? s.existing.band.photo_rect,
        blocks: s.existing.blocks || [],
        brushAdd: d.brushAdd || null,
        brushRemove: d.brushRemove || null,
        grow: 2,
      }
    }
    if (s.existing && (s.existing.case === 'B' || s.existing.case === 'C') && d.mode !== 'band' && s.existing.text) {
      job.original_text = { case: s.existing.case, confidence: s.existing.confidence, text: s.existing.text }
    } else if (s.existing?.case === 'A' && s.existing.originalText) {
      job.original_text = s.existing.originalText
    }
    const gen = s.layoutGen
    const lay = (await this.layoutNow(s)) ?? s.layout!
    if (gen === s.layoutGen) s.layout = lay
    job.layout = stripLayout(lay)
    const tiles = await renderTextTiles(lay)
    job.tiles = tiles.map((t, i) => ({ x: t.x, y: t.y, name: `tile${i}` }))
    return { job, tiles, snap }
  }

  // ------------------------------------------------------------------ navigation
  next(): Promise<void> {
    if (this.current < this.photos.length - 1) return this.select(this.current + 1)
    return Promise.resolve()
  }
  prev(): Promise<void> {
    if (this.current > 0) return this.select(this.current - 1)
    return Promise.resolve()
  }
}

export function eraseBandRect(pr: Rect, W: number, H: number): Rect {
  const [x, y, w, h] = pr
  const cands: Rect[] = [
    [0, y + h, W, H - y - h],
    [0, 0, W, y],
    [x + w, 0, W - x - w, H],
    [0, 0, x, H],
  ]
  cands.sort((a, b) => b[2] * b[3] - a[2] * a[3])
  return cands[0]
}

function stripLayout(l: LayoutResult) {
  const { blockBoxes, warnings, scale, ...rest } = l as any
  return rest
}

function basename(p: string) {
  return p.split(/[\\/]/).pop() || p
}

function escapeMk(s: string) {
  return s.replace(/\\/g, '\\\\').replace(/\*/g, '\\*')
}

export const app = new AppStore()
export { plainText }
