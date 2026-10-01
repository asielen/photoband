// Regressions from verifying the merged review fixes: case C Overwrite in every mode, batch drafts
// and the file version they were made for, batch case C planning, the desktop file dialog, and
// the "unsaved edits from an earlier version" toast after an overwrite. Harness as in
// review_store.test.ts; drafts follow the real server: /api/photo/meta returns a draft only when
// it was made for the file as it is now, /api/drafts/get returns it anyway.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const server: any = {}
function resetServer() {
  Object.assign(server, {
    drafts: {} as Record<string, any>,
    saveGate: null as null | (() => void),
    recordOnDisk: false,
    existingCase: null as any,
    keepDraftOnSave: false,
    saves: 0,
    dialog: null as null | (() => any),
    posts: [] as { path: string; body: any }[],
  })
}
resetServer()

vi.mock('../lib/api', () => {
  class ApiError extends Error {
    status: number
    constructor(m: string, status: number) { super(m); this.status = status }
  }
  const stat = () => [1, server.recordOnDisk ? '2' : '1', 'h']
  const sameStat = (a: any, b: any) => !!a && String(a[0]) === String(b[0]) && String(a[1]) === String(b[1])
  const validDraft = (path: string) => {
    const d = server.drafts[path]
    return d && (!d._stat || sameStat(d._stat, stat())) ? d : null
  }
  const meta = (path: string) => ({
    path, name: 'a.tif', info: { upright_width: 1000, upright_height: 800, channels: 3, dpi: [300, 300], pages: 1, format: 'TIFF' },
    fields: {}, stat: stat(), hasRecord: server.recordOnDisk, warnings: [], draft: validDraft(path),
  })
  return {
    ApiError,
    TOKEN: 't',
    get: vi.fn(async (p: string, q: any) => {
      if (p === '/api/photo/meta') return meta(q.path)
      if (p === '/api/drafts/get') {
        const d = server.drafts[q.path] ?? null
        return { state: d, valid: !d || !!validDraft(q.path) }
      }
      return {}
    }),
    post: vi.fn(async (p: string, b: any) => {
      server.posts.push({ path: p, body: b })
      if (p === '/api/dialog') return server.dialog ? server.dialog() : { paths: [] }
      if (p === '/api/drafts') { server.drafts[b.path] = b.state; return { ok: true } }
      if (p === '/api/settings') return {}
      if (p === '/api/resolve') return { b1: { text: '', empty_tokens: [] } }
      if (p === '/api/photo/existing') {
        if (server.existingCase) return server.existingCase
        if (!server.recordOnDisk) return { case: null, warnings: [] }
        return { case: 'A', sourceRect: [60, 60, 880, 600], band: { photo_rect: [60, 60, 880, 600] },
                 state: { templateId: 'tpl', blocks: [{ id: 'b1', text: 'Grandma', custom: true }], overrides: {} },
                 record: { mode: 'band' } }
      }
      return { ok: true }
    }),
    postForm: vi.fn(async (_p: string, _form: FormData) => {
      server.saves++
      await new Promise<void>((r) => (server.saveGate = r))
      server.recordOnDisk = true
      server.existingCase = null
      if (!server.keepDraftOnSave) delete server.drafts['/p/a.tif']
      return { ok: true, path: '/p/a.tif', out_path: '/p/a.tif', backup_path: '/p/_originals/a.tif' }
    }),
  }
})
vi.mock('../lib/fonts', async () => {
  const real: any = await vi.importActual('../lib/fonts')
  return { ...real, ensureFonts: async () => {}, loadRegistry: async () => {}, resolveFont: (id: string) => ({ id, missing: false }) }
})
vi.mock('../lib/render', () => ({ renderTextTiles: async () => [] }))
vi.mock('../lib/layout', async () => {
  const real: any = await vi.importActual('../lib/layout')
  return { ...real, clearMeasureCache: () => {},
    computeLayout: (inp: any) => ({ mode: inp.mode, sourceRect: inp.sourceRect, canvas: [1, 1], photoRect: [0, 0, 1, 1], fills: [], runs: [], textAreas: [], textColors: [], warnings: [] }) }
})

const style = { font: 'x', weight: 400, italic: false, size: 2, color: '#111111', align: 'left', lineHeight: 1.25, letterSpacing: 0, spaceBefore: 0, spaceAfter: 0, case: 'none' }
const tpl: any = { id: 'tpl', name: 'T', builtin: true, scaleMode: 'relative', layout: {}, blocks: [{ id: 'b1', name: 'B', format: '', style }] }
const flush = () => new Promise((r) => setTimeout(r, 0))
const CASE_C = { case: 'C', band: { photo_rect: [0, 0, 1000, 700], band_color_hex: '#ffffff' }, blocks: [], text: 'Rose 1952', confidence: 0.9, warnings: [] }

let lastApp: any = null
async function open(saving: any = {}) {
  const { app } = await import('../lib/store.svelte')
  lastApp = app
  app.mode = 'browser'
  app.settings = { general: {}, session: {}, saving: { allowMultipageSave: false, ...saving }, batch: {} } as any
  app.templates = [tpl] as any
  app.sessions.clear()
  app.photos = [{ path: '/p/a.tif', name: 'a.tif', status: 'untouched' }]
  app.current = -1
  app.toasts = []
  app.overwriteConfirmed = app.settings.saving.backupOriginals ? 'backup' : 'nobackup'
  await app.select(0)
  await app.existingReady(app.session!)
  return { app, s: app.session! }
}

describe('verified review findings', () => {
  beforeEach(() => { resetServer(); vi.resetModules() })
  afterEach(() => {
    for (const s of lastApp?.sessions.values() ?? []) clearTimeout(s.draftTimer)
    lastApp = null
  })

  // ---- "Don't ask again" holds only for the backup setting it was given under
  it('turning backups off asks again before the next overwrite', async () => {
    const { app } = await open({ backupOriginals: true })
    app.overwriteConfirmed = 'backup'
    app.settings.saving.backupOriginals = false   // one click on the toolbar switch
    const { dialogs } = await import('../lib/dialogs.svelte')
    const ask = vi.spyOn(dialogs, 'ask').mockResolvedValue({ id: 'cancel' } as any)
    const { actions } = await import('../lib/actions')
    expect(await actions.overwrite()).toBe(false)
    expect(ask).toHaveBeenCalledTimes(1)
    expect(String(ask.mock.calls[0][1])).toMatch(/Backups are turned OFF/)
    expect(server.saves).toBe(0)
    ask.mockRestore()
  })

  // ---- 1. case C: Overwrite is off in every mode
  it('case C with "Ignore" (a new band below the handwriting) cannot be overwritten', async () => {
    server.existingCase = CASE_C
    const { app, s } = await open()
    s.existingIgnored = true // "Ignore": keep the handwriting, add a band
    expect(s.draft.mode).toBe('band')
    expect(app.isCaseC(s)).toBe(true)
    expect(app.overwriteBlocked(s)).toMatch(/handwritten or printed caption, so overwriting it is turned off.*Settings › Saving/)
    const { actions } = await import('../lib/actions')
    expect(await actions.overwrite()).toBe(false)
    expect(server.saves).toBe(0)
    expect(app.toasts.some((t: any) => /overwriting it is turned off/.test(t.text))).toBe(true)
    // erase and replace are protected the same way
    app.useExisting(s, 'template', 'erase')
    expect(app.overwriteBlocked(s)).not.toBeNull()
  })

  it('case C can be overwritten once Settings › Saving allows it; other cases are not affected', async () => {
    server.existingCase = CASE_C
    const { app, s } = await open({ allowOverwriteHandwritten: true })
    s.existingIgnored = true
    expect(app.overwriteBlocked(s)).toBeNull()
    server.existingCase = { case: 'B', band: { photo_rect: [0, 0, 1000, 700] }, blocks: [], warnings: [] }
    const b = await open()
    expect(b.app.overwriteBlocked(b.s)).toBeNull()
  })

  // ---- 2. drafts for another version of the file (shared by the editor and the batch)
  it('draftForFile keeps only text and style from a draft made for another file version', async () => {
    const { draftForFile } = await import('../lib/store.svelte')
    const base: any = { templateId: 'tpl', overrides: {}, blocks: {}, mode: 'erase', sourceRect: null, photoRect: [5, 5, 900, 600], existingChoice: 'template' }
    const stored = { templateId: 'tpl', overrides: { layout: { x: 1 } }, blocks: { b1: { id: 'b1', custom: true, text: 'words' } },
      mode: 'band', sourceRect: [1, 2, 3, 4], photoRect: [1, 2, 3, 4], _stat: [1, 'old', 'h'], _hash: 'abc' }
    const has = (id: string) => id === 'tpl'
    const stale = draftForFile(stored, [1, 'new', 'h'], base, has)
    expect(stale.stale).toBe(true)
    expect(stale.hash).toBe('abc')
    expect(stale.draft.mode).toBe('erase')
    expect(stale.draft.photoRect).toEqual([5, 5, 900, 600])
    expect(stale.draft.sourceRect).toBeNull()
    expect(stale.draft.blocks.b1.text).toBe('words')
    expect((stale.draft.overrides as any).layout.x).toBe(1)
    expect('_stat' in stale.draft).toBe(false)
    expect('_hash' in stale.draft).toBe(false)
    const same = draftForFile(stored, [1, 'old', 'h'], base, has)
    expect(same.stale).toBe(false)
    expect(same.draft.mode).toBe('band')
    expect(same.draft.sourceRect).toEqual([1, 2, 3, 4])
    expect('_stat' in same.draft).toBe(false)
    // an unknown template falls back to the base one
    expect(draftForFile({ ...stored, templateId: 'gone' }, [1, 'old', 'h'], base, has).draft.templateId).toBe('tpl')
  })

  // ---- 4. batch case C planning matches the server
  it('an overwrite batch plans case C as erased on a copy, whatever Settings › Saving allows', async () => {
    const { planCaseC, caseCOverwriteRefused } = await import('../lib/batchplan')
    const bs = { caseC: 'erase', which: 'all', saveMode: 'overwrite' } as any
    expect(planCaseC(bs)).toEqual({ action: 'erase', onCopy: true })
    expect(planCaseC({ ...bs, saveMode: 'copy' })).toEqual({ action: 'erase', onCopy: false })
    expect(planCaseC({ ...bs, caseC: 'skip' }).action).toBe('skip')
    expect(planCaseC({ ...bs, which: 'uncaptioned' }).action).toBe('skip')
    // erasing goes to a copy: never refused
    expect(caseCOverwriteRefused('erase', bs, false)).toBeNull()
    // a draft that keeps the handwriting would overwrite the scan: refused unless allowed
    expect(caseCOverwriteRefused('band', bs, false)).toMatch(/overwriting it is off/)
    expect(caseCOverwriteRefused('band', bs, true)).toBeNull()
    expect(caseCOverwriteRefused('band', { ...bs, saveMode: 'copy' }, false)).toBeNull()
  })

  // ---- 3. the file dialog
  it('desktop: a failing system dialog shows the error and never opens the in-app browser', async () => {
    const { app } = await open()
    const { dialogs } = await import('../lib/dialogs.svelte')
    const { ApiError } = (await import('../lib/api')) as any
    app.mode = 'desktop'
    server.dialog = () => { throw new ApiError('dialog crashed', 500) }
    expect(await dialogs.pick('open-files', 'Open photos')).toBeNull()
    expect(dialogs.browserState).toBeNull()
    expect(app.toasts.some((t: any) => t.kind === 'error' && /dialog crashed/.test(t.text))).toBe(true)
    app.toasts = []
    server.dialog = () => ({ unavailable: true })
    expect(await dialogs.pick('open-folder')).toBeNull()
    expect(dialogs.browserState).toBeNull()
    expect(app.toasts.some((t: any) => t.kind === 'error')).toBe(true)
    expect(server.posts.some((p: any) => p.path.startsWith('/api/fs/'))).toBe(false)
  })

  it('browser mode: a refused request (4xx) is shown, a server error falls back to the in-app browser', async () => {
    const { app } = await open()
    const { dialogs } = await import('../lib/dialogs.svelte')
    const { ApiError } = (await import('../lib/api')) as any
    server.dialog = () => { throw new ApiError('Unknown dialog kind', 400) }
    expect(await dialogs.pick('open-files')).toBeNull()
    expect(dialogs.browserState).toBeNull()
    expect(app.toasts.some((t: any) => /Unknown dialog kind/.test(t.text))).toBe(true)
    server.dialog = () => { throw new ApiError('boom', 500) }
    const p = dialogs.pick('open-files')
    while (!dialogs.browserState) await flush()
    dialogs.closeBrowser(null)
    expect(await p).toBeNull()
  })

  // ---- 5. no stale-draft toast for text already carried over after an overwrite
  it('edits autosaved during an overwrite are carried over without an "earlier version" toast', async () => {
    server.keepDraftOnSave = true
    const { app, s } = await open()
    app.setBlockText(s, 'b1', 'Grandma Rose')
    const p = app.save(s, 'overwrite')
    while (!server.saveGate) await flush()
    app.setBlockText(s, 'b1', 'Grandma Rose, 1952') // typed while the save runs
    await app.flushDraft(s) // ...and autosaved for the file before the overwrite
    expect(server.drafts['/p/a.tif']._stat).toEqual([1, '1', 'h'])
    server.saveGate!()
    await p
    const fresh = app.session!
    await app.existingReady(fresh)
    await flush(); await flush()
    expect(app.blockText(fresh, 'b1')).toBe('Grandma Rose, 1952')
    expect(fresh.draft.mode).toBe('rebuild')
    expect(app.toasts.some((t: any) => /earlier version/.test(t.text))).toBe(false)
    expect(server.posts.some((x: any) => x.path === '/api/drafts/get')).toBe(false)
  })

  it('a stale draft is still offered when a photo is opened normally', async () => {
    server.recordOnDisk = true
    server.drafts['/p/a.tif'] = { templateId: 'tpl', overrides: {}, blocks: { b1: { id: 'b1', custom: true, text: 'old words' } },
      mode: 'band', sourceRect: null, photoRect: null, _stat: [1, '1', 'h'] }
    const { app } = await open()
    await flush()
    expect(app.toasts.some((t: any) => /earlier version/.test(t.text))).toBe(true)
  })
})
