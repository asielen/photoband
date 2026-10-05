// Store integrity regressions (review findings): a second band stacked onto an existing one,
// stale geometry after reloads, resolve races, file templates, drafts on close, plain wording,
// unequal X/Y DPI. The backend, fonts, rendering and (by default) the layout engine are mocked.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const server: any = {}
function resetServer() {
  Object.assign(server, {
    drafts: {} as Record<string, any>,
    saveGate: null as null | (() => void),
    metaCalls: 0,
    recordOnDisk: false,
    stat: null as any,
    resolveDelay: {} as Record<string, number>,
    existingDelay: 0,
    existingFail: false,
    existingCase: null as any,
    keepDraftOnSave: false,
    lastJob: null as any,
    staleDraft: null as any,
    recordTemplate: null as any,
    dpi: [300, 300],
    warnings: [] as string[],
    faces: null as any,
    fields: {} as any,
    saveOut: null as string | null,
    details: [] as any[],
    realLayout: false,
    posts: [] as { path: string; body: any }[],
  })
}
resetServer()

vi.mock('../lib/api', () => {
  class ApiError extends Error {}
  const stat = () => server.stat ?? [1, server.recordOnDisk ? '2' : '1', 'h']
  const meta = (path: string) => ({
    path, name: 'a.tif', info: { upright_width: 1000, upright_height: 800, channels: 3, dpi: server.dpi, pages: 1, format: 'TIFF' },
    fields: server.fields, stat: stat(), hasRecord: server.recordOnDisk, warnings: server.warnings,
    faces: server.faces ?? { named: [], unnamed: [], unnamed_count: 0, has_positions: false, warnings: [] },
    draft: server.drafts[path] ?? null,
  })
  const settings = () => ({ general: {}, session: {}, saving: { allowMultipageSave: false } })
  return {
    ApiError,
    TOKEN: 't',
    get: vi.fn(async (p: string, q: any) => {
      if (p === '/api/photo/meta') { server.metaCalls++; return meta(q.path) }
      if (p === '/api/drafts/get') return { state: server.staleDraft, valid: !server.staleDraft }
      if (p === '/api/templates') return [tpl, tpl2]
      return {}
    }),
    post: vi.fn(async (p: string, b: any) => {
      server.posts.push({ path: p, body: b })
      if (p === '/api/drafts') { server.drafts[b.path] = b.state; return { ok: true } }
      if (p === '/api/settings') return settings()
      if (p === '/api/photo/details') {
        server.details.push(b)
        server.fields = { ...server.fields, ...b.edits }
        server.stat = [2, '9', 'h2']
        return { ok: true, path: b.path, out_path: b.path, backup_path: '', meta: meta(b.path), notes: [] }
      }
      if (p === '/api/resolve') {
        const d = (server.resolveDelay || {})[b.templateName] || 0
        if (d) await new Promise((r) => setTimeout(r, d))
        return { b1: { text: 'resolved by ' + b.templateName, empty_tokens: [] } }
      }
      if (p === '/api/photo/existing') {
        if (server.existingDelay) await new Promise((r) => setTimeout(r, server.existingDelay))
        if (server.existingFail) throw new Error('OCR engine crashed')
        if (server.existingCase) return server.existingCase
        if (!server.recordOnDisk) return { case: null, warnings: [] }
        return { case: 'A', sourceRect: [60, 60, 880, 600], band: { photo_rect: [60, 60, 880, 600] },
                 state: { templateId: server.recordTemplate?.id ?? 'tpl', template: server.recordTemplate, blocks: [{ id: 'b1', text: 'Grandma', custom: true }], overrides: {} },
                 record: { mode: 'band' } }
      }
      return { ok: true }
    }),
    postForm: vi.fn(async (_p: string, form: FormData) => {
      server.lastJob = JSON.parse(form.get('job') as string)
      await new Promise<void>((r) => (server.saveGate = r))
      server.recordOnDisk = true
      if (!server.keepDraftOnSave) delete server.drafts['/p/a.tif'] // the server deletes the draft it saved
      const out = server.saveOut ?? '/p/a.tif'
      return { ok: true, path: '/p/a.tif', out_path: out, backup_path: out === '/p/a.tif' ? '/p/_originals/a.tif' : '' }
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
    computeLayout: (inp: any) => server.realLayout ? real.computeLayout(inp)
      : ({ mode: inp.mode, sourceRect: inp.sourceRect, canvas: [1, 1], photoRect: [0, 0, 1, 1], fills: [], runs: [], textAreas: [], textColors: [], warnings: [] }) }
})

const style = { font: 'x', weight: 400, italic: false, size: 2, color: '#111111', align: 'left', lineHeight: 1.25, letterSpacing: 0, spaceBefore: 0, spaceAfter: 0, case: 'none' }
const tpl2: any = { id: 'tpl2', name: 'T2', builtin: true, scaleMode: 'relative', layout: {}, blocks: [{ id: 'b1', name: 'B', format: '{date}', style }] }
const tpl: any = { id: 'tpl', name: 'T', builtin: true, scaleMode: 'relative', layout: {}, blocks: [{ id: 'b1', name: 'B', format: '', style }] }
const flush = () => new Promise((r) => setTimeout(r, 0))
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

let lastApp: any = null
async function open(waitExisting = true) {
  const { app } = await import('../lib/store.svelte')
  lastApp = app
  const { dialogs } = await import('../lib/dialogs.svelte')
  app.settings = { general: {}, session: {}, saving: { allowMultipageSave: false } } as any
  app.templates = [tpl, tpl2] as any
  app.sessions.clear()
  app.photos = [{ path: '/p/a.tif', name: 'a.tif', status: 'untouched' }, { path: '/p/b.tif', name: 'b.tif', status: 'untouched' }]
  app.current = -1
  app.toasts = []
  await app.select(0)
  if (waitExisting) await app.existingReady(app.session!)
  return { app, dialogs, s: app.session! }
}

describe('store integrity (review findings)', () => {
  beforeEach(() => { resetServer(); vi.resetModules() })
  // a draft autosave still pending from one test must not land in the next one
  afterEach(() => {
    for (const s of lastApp?.sessions.values() ?? []) clearTimeout(s.draftTimer)
    lastApp = null
  })

  // ---- 1. saving while the existing-caption check runs
  it('cannot save while the existing-caption check is running; a save waits for it', async () => {
    server.recordOnDisk = true
    server.existingDelay = 80
    const { app, s } = await open(false)
    expect(s.existingLoading).toBe(true)
    expect(app.canSave(s)).toMatch(/Checking for an existing caption/)
    const p = app.save(s, 'copy')
    while (!server.saveGate) await sleep(10)
    // the save waited: the photo is the old photo area, not the whole captioned image
    expect(server.lastJob.layout.sourceRect).toEqual([60, 60, 880, 600])
    expect(server.lastJob.case).toBe('A')
    server.saveGate()
    await p
  })

  // ---- 2. erase draft without a band
  it('refuses to save an Erase-in-place draft when the existing-caption analysis failed', async () => {
    server.existingFail = true
    server.drafts['/p/a.tif'] = { templateId: 'tpl', overrides: {}, blocks: { b1: { id: 'b1', custom: true, text: 'x' } },
      mode: 'erase', sourceRect: null, photoRect: [0, 0, 1000, 700], existingChoice: 'template' }
    const { app, s } = await open()
    expect(s.draft.mode).toBe('erase')
    expect(app.canSave(s)).toMatch(/erase the old caption/)
    await expect(app.buildJob(s, 'copy')).rejects.toThrow(/erase the old caption/)
    // the way out: add a new band instead
    app.leaveExisting(s)
    expect(app.canSave(s)).toBeNull()
  })

  it('refuses to save a rebuild draft when the analysis found no caption', async () => {
    server.drafts['/p/a.tif'] = { templateId: 'tpl', overrides: {}, blocks: {}, mode: 'rebuild', sourceRect: [10, 10, 500, 400], photoRect: [10, 10, 500, 400] }
    const { app, s } = await open()
    expect(app.canSave(s)).toMatch(/replace an old caption/)
  })

  // ---- 3b. typing before the analysis returns
  it('typing before the existing-caption analysis returns still edits the case-A caption (text kept)', async () => {
    server.recordOnDisk = true
    server.existingDelay = 100
    const { app, s } = await open(false)
    app.setBlockText(s, 'b1', 'Grandma Rose')
    await app.existingReady(s)
    expect(s.existing?.case).toBe('A')
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.blockText(s, 'b1')).toBe('Grandma Rose')
    // undoing the typing never goes back to band mode
    app.undo(s)
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.canSave(s)).toBeNull()
  })

  // ---- 4. resolve race
  it('a slow earlier resolve never overwrites the current template text', async () => {
    const { app, s } = await open()
    server.resolveDelay = { T2: 50 }
    const a = app.setTemplate(s, 'tpl2') // slow
    const b = app.setTemplate(s, 'tpl') // fast, the user's final choice
    await Promise.all([a, b])
    expect(s.draft.templateId).toBe('tpl')
    expect(app.blockText(s, 'b1')).toBe('resolved by T')
    // the last template remembered is the final choice
    const lastTpl = server.posts.filter((x: any) => x.path === '/api/settings').map((x: any) => x.body.session.lastTemplate)
    expect(lastTpl[lastTpl.length - 1]).toBe('tpl')
  })

  // ---- 3a. edits typed during an overwrite
  it('overwrite with edits typed during the save: the fresh session edits the new band (no stacked band)', async () => {
    const { app, s } = await open()
    app.setBlockText(s, 'b1', 'Grandma Rose')
    const p = app.save(s, 'overwrite')
    await flush(); await flush()
    app.setBlockText(s, 'b1', 'Grandma Rose, 1952') // typed while the save runs
    server.saveGate!()
    await p
    const fresh = app.session!
    await app.existingReady(fresh)
    await flush()
    expect(fresh).not.toBe(s)
    expect(fresh.draft.mode).toBe('rebuild')
    expect(fresh.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.blockText(fresh, 'b1')).toBe('Grandma Rose, 1952')
    expect(app.canSave(fresh)).toBeNull()
    // the retired session never writes its (pre-overwrite) geometry as a draft
    await sleep(900)
    const d = server.drafts['/p/a.tif']
    if (d) expect(d.mode).not.toBe('band')
  })

  it('a draft written for the previous version of the file only gives back its text', async () => {
    server.keepDraftOnSave = true // the server keeps a newer draft than the one saved
    const { app, s } = await open()
    app.setBlockText(s, 'b1', 'Grandma Rose')
    const p = app.save(s, 'overwrite')
    await flush(); await flush()
    app.setBlockText(s, 'b1', 'Grandma Rose, 1952')
    await app.flushDraft(s) // the autosave fires during the save
    expect(server.drafts['/p/a.tif']._stat).toEqual([1, '1', 'h'])
    server.saveGate!()
    await p
    const fresh = app.session!
    await app.existingReady(fresh)
    await flush()
    expect(fresh.draft.mode).toBe('rebuild')
    expect(fresh.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.blockText(fresh, 'b1')).toBe('Grandma Rose, 1952')
  })

  // ---- 3c. undo
  it('undo past the automatic "edit existing caption" step keeps the case-A geometry', async () => {
    server.recordOnDisk = true
    const { app, s } = await open()
    expect(s.draft.mode).toBe('rebuild')
    app.setBlockText(s, 'b1', 'typo')
    await sleep(700)
    app.undo(s) // undo the typo
    app.undo(s) // one press too many: nothing left to undo
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
  })

  it('"Add a new band" on case A is explicit and allowed; band mode without it is blocked', async () => {
    server.recordOnDisk = true
    const { app, s } = await open()
    app.leaveExisting(s)
    expect(s.draft.mode).toBe('band')
    expect(s.draft.keepBand).toBe(true)
    expect(app.canSave(s)).toBeNull()
    s.draft = { ...s.draft, keepBand: false }
    expect(app.canSave(s)).toMatch(/already has a Photoband caption/)
    await expect(app.buildJob(s, 'copy')).rejects.toThrow(/already has a Photoband caption/)
  })

  // ---- 3d. reload paths
  it('a file changed on disk resets the geometry and re-runs the analysis', async () => {
    server.existingCase = { case: 'B', band: { photo_rect: [10, 10, 500, 400] }, blocks: [], warnings: [] }
    const { app, s } = await open()
    app.useExisting(s, 'template', 'rebuild')
    expect(s.draft.sourceRect).toEqual([10, 10, 500, 400])
    app.setBlockText(s, 'b1', 'kept text')
    // another app re-captioned it with Photoband
    server.existingCase = null
    server.recordOnDisk = true
    await app.checkChanged(s)
    expect(s.meta!.stat).toEqual([1, '2', 'h'])
    expect(s.existing?.case).toBe('A')
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.blockText(s, 'b1')).toBe('kept text')
    // no undo step brings back the old file's photo edge
    while (s.undoStack.length) {
      app.undo(s)
      expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
    }
  })

  it('"Use them" on a stale draft takes its text, not its photo edge', async () => {
    server.recordOnDisk = true
    server.staleDraft = { templateId: 'tpl', overrides: {}, blocks: { b1: { id: 'b1', custom: true, text: 'old words' } }, mode: 'band', sourceRect: null, photoRect: null }
    const { app, s } = await open()
    await flush()
    const t = app.toasts.find((x) => x.action?.label === 'Use them')!
    expect(t).toBeTruthy()
    await t.action!.run()
    await flush()
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.sourceRect).toEqual([60, 60, 880, 600])
    expect(app.blockText(s, 'b1')).toBe('old words')
  })

  // ---- 5. file templates
  it('a template embedded in a file survives reloading templates and is saved back under its own id', async () => {
    server.recordOnDisk = true
    server.recordTemplate = { ...tpl, id: 'custom', name: 'Family', builtin: false }
    const { app, s } = await open()
    const id = s.draft.templateId
    expect(id).not.toBe('tpl')
    expect(app.template(id)?.fromFile?.id).toBe('custom')
    await app.templatesChanged()
    expect(s.draft.templateId).toBe(id)
    expect(app.toasts.some((t) => /no longer exists/.test(t.text))).toBe(false)
    const { job } = await app.buildJob(s, 'copy')
    expect(job.state.templateId).toBe('custom')
    expect(job.state.template.id).toBe('custom')
    expect(job.state.template.name).toBe('Family')
    expect(job.state.template.fromFile).toBeUndefined()
  })

  it('two files embedding different versions of one template id each keep their own', async () => {
    server.recordOnDisk = true
    server.recordTemplate = { ...tpl, id: 'custom', name: 'Family', builtin: false }
    const { app, s } = await open()
    const first = s.draft.templateId
    server.recordTemplate = { ...tpl, id: 'custom', name: 'Family', builtin: false, layout: { changed: true } }
    await app.select(1)
    const s2 = app.session!
    await app.existingReady(s2)
    expect(s2.draft.templateId).not.toBe(first)
    expect((app.template(s2.draft.templateId)!.layout as any).changed).toBe(true)
    expect((app.template(first)!.layout as any).changed).toBeUndefined()
  })

  it('a session whose template disappears is switched with a notice', async () => {
    const { app, s } = await open()
    app.templates = [...app.templates, { ...tpl, id: 'gone', name: 'Gone' }]
    await app.setTemplate(s, 'gone')
    await app.templatesChanged()
    expect(s.draft.templateId).not.toBe('gone')
    expect(app.toasts.some((t) => /no longer exists/.test(t.text))).toBe(true)
  })

  // ---- 6. unsaved edits
  it('navigating away from unsaved edits says they are kept as a draft', async () => {
    const { app, s } = await open()
    app.setBlockText(s, 'b1', 'edited')
    await app.select(1)
    expect(app.toasts.some((t) => t.text === 'Edits to a.tif are kept as a draft — not yet saved to the file.')).toBe(true)
    expect(server.drafts['/p/a.tif'].blocks.b1.text).toBe('edited')
  })

  it('Close all flushes drafts and asks when edits are unsaved', async () => {
    const { app, dialogs, s } = await open()
    app.setBlockText(s, 'b1', 'edited')
    let p = app.closeAllPhotos()
    while (!dialogs.confirmState) await flush()
    expect(server.drafts['/p/a.tif'].blocks.b1.text).toBe('edited')
    expect(dialogs.confirmState.message).toMatch(/a\.tif/)
    dialogs.close('cancel')
    expect(await p).toBe(false)
    expect(app.photos.length).toBe(2)
    p = app.closeAllPhotos()
    while (!dialogs.confirmState) await flush()
    dialogs.close('ok')
    expect(await p).toBe(true)
    expect(app.photos.length).toBe(0)
  })

  it('flushAll writes pending drafts (window close)', async () => {
    const { app, s } = await open()
    app.setBlockText(s, 'b1', 'last words')
    expect(server.drafts['/p/a.tif']).toBeUndefined()
    await app.flushAll()
    expect(server.drafts['/p/a.tif'].blocks.b1.text).toBe('last words')
  })

  // ---- 7. wording
  it('metadata warnings are in plain language', async () => {
    server.warnings = ['no face regions', 'region dimensions mismatch']
    const { app, s } = await open()
    app.templates = [{ ...tpl, blocks: [{ id: 'b1', name: 'People', format: '{names}', style }] }, tpl2]
    let msgs = app.warnings(s).map((w) => w.message)
    expect(msgs).toContain('No names are tagged on faces in this photo, so the People line is blank. Turn on Faces below the photo to name them.')
    for (const m of msgs) expect(m).not.toMatch(/[{}]|PersonInImage|RegionInfo|XMP|IPTC|EXIF/)
    // a name tagged here (no warning any more), then one without a place on the photo
    app.addFace(s, [0.1, 0.1, 0.1, 0.1], 'Ann')
    msgs = app.warnings(s).map((w) => w.message)
    expect(msgs.some((m) => m.startsWith('No names'))).toBe(false)
    s.meta!.faces = { named: [{ name: 'Bea', box: null, source: 'PersonInImage', ids: ['pii:0'], key: 'pii:0' }], unnamed: [], unnamed_count: 0, has_positions: false, warnings: [] }
    msgs = app.warnings(s).map((w) => w.message)
    expect(msgs).toContain('Some names have no place on the photo, so the left-to-right order may be wrong. Mark them in the People list (Metadata tab).')
    for (const m of msgs) expect(m).not.toMatch(/[{}]|PersonInImage|RegionInfo|XMP|IPTC|EXIF/)
  })

  // ---- 8. unequal DPI
  it('physical sizes use the horizontal DPI for widths and the vertical DPI for heights', async () => {
    server.dpi = [300, 600]
    const { app, s } = await open()
    const inp: any = app.layoutInput(s)
    expect(inp.dpi).toBe(300)
    expect(inp.dpiY).toBe(600)
    server.realLayout = true
    const mono = {
      width: (t: string, font: string) => t.length * 0.5 * parseFloat(/([\d.]+)px/.exec(font)![1]),
      metrics: (font: string) => { const z = parseFloat(/([\d.]+)px/.exec(font)![1]); return { ascent: z * 0.8, descent: z * 0.2 } },
    }
    const { computeLayout } = await import('../lib/layout')
    const t: any = {
      id: 'p', name: 'P', scaleMode: 'physical',
      layout: {
        border: { top: 10, right: 10, bottom: 0, left: 10 }, lockSides: false, borderColor: '#ffffff', bandColor: '#ffffff', linkColors: true,
        bandHeight: { mode: 'auto', min: 0, overflow: 'warn' }, padding: { top: 0, right: 0, bottom: 0, left: 0 }, textMaxWidth: 100,
        vAlign: 'top', columns: { count: 1, split: 60, gutter: 4 }, divider: { enabled: false, width: 0, color: '#000', inset: 0 },
        keyline: { enabled: false, width: 0, color: '#000' },
      },
      blocks: [{ id: 'a', name: 'A', format: '', column: 0, style: { ...style, size: 12 } }],
    }
    const l = computeLayout({ template: t, sourceRect: [0, 0, 1000, 800], dpi: 300, dpiY: 600, texts: { a: 'Hi' }, mode: 'band', measurer: mono } as any)
    // 10 mm: 118 px across at 300 dpi, 236 px down at 600 dpi
    expect(l.photoRect).toEqual([118, 236, 1000, 800])
    // 12 pt is a height: 100 px at 600 dpi
    expect(l.runs[0].size).toBeCloseTo(100, 1)
  })
})

describe('edited photo details', () => {
  beforeEach(() => { resetServer(); vi.resetModules() })
  afterEach(() => {
    for (const s of lastApp?.sessions.values() ?? []) { clearTimeout(s.draftTimer); clearTimeout(s.resolveTimer) }
    lastApp = null
  })

  it('a detail is an edit only while it differs from the file, compared exactly', async () => {
    server.fields = { title: 'Picnic' }
    const { app, s } = await open()
    app.setDetail(s, 'title', 'Picnic ')
    expect(s.draft.meta).toEqual({ title: 'Picnic ' })   // the space typed before the next word stays
    app.setDetail(s, 'title', 'Picnic')
    expect(s.draft.meta).toBeUndefined()
    app.setDetail(s, 'city', 'Paris')
    expect(s.dirty).toBe(true)
    app.undo(s)
    expect(s.draft.meta).toBeUndefined()
  })

  it('captions are resolved with the edits, and a resolve made before an edit no longer counts', async () => {
    const { app, s } = await open()
    app.setDetail(s, 'title', 'Picnic')
    await sleep(200)
    const r = server.posts.filter((x: any) => x.path === '/api/resolve').pop()!
    expect(r.body.edits).toEqual({ title: 'Picnic' })
    const job: any = (await app.buildJob(s, 'copy')).job
    expect(job.meta_edits).toEqual({ title: 'Picnic' })
    expect(job.state.faceRows).toBeNull()
  })

  it('details survive the case A default (the draft is rebuilt from the record)', async () => {
    server.recordOnDisk = true
    server.drafts['/p/a.tif'] = { templateId: 'tpl', overrides: {}, blocks: {}, mode: 'band', sourceRect: null, photoRect: null,
      meta: { title: 'Kept' }, faceRows: 2, _stat: [1, '2', 'h'] }
    const { s } = await open()
    expect(s.draft.mode).toBe('rebuild')
    expect(s.draft.meta).toEqual({ title: 'Kept' })
    expect(s.draft.faceRows).toBe(2)
  })

  it('a saved copy keeps the details for the original and offers to write them', async () => {
    server.saveOut = '/p/captioned/a.tif'
    const { app, s } = await open()
    app.setDetail(s, 'title', 'Picnic')
    const p = app.save(s, 'copy')
    while (!server.saveGate) await sleep(10)
    server.saveGate()
    await p
    await flush()
    const d = server.drafts['/p/a.tif']
    expect(d.meta).toEqual({ title: 'Picnic' })
    expect(d.blocks).toEqual({})
    expect(app.toasts.some((t: any) => t.action?.label === 'Save to original')).toBe(true)
  })

  it('save to original writes the details, and nothing is left unsaved when they were all there was', async () => {
    server.fields = { title: 'Old' }
    const { app, s } = await open()
    app.setDetail(s, 'title', 'New')
    expect(await app.saveDetails(s)).toBe(true)
    expect(server.details[0].edits).toEqual({ title: 'New' })
    expect(server.details[0].expected_stat).toEqual([1, '1', 'h'])
    expect(s.draft.meta).toBeUndefined()
    expect(s.meta!.fields.title).toBe('New')
    expect(s.dirty).toBe(false)
    expect(s.undoStack.some((x: string) => x.includes('"meta"'))).toBe(false)
    expect(server.posts.some((x: any) => x.path === '/api/drafts' && x.body.state === null)).toBe(true)
  })

  it('face edits: a rename back is no edit; renames follow the people keywords', async () => {
    const faces = { named: [{ name: 'Ann', box: [0.1, 0.1, 0.1, 0.1], source: 'MWG', ids: ['mwg:0'], key: 'mwg:0' }],
      unnamed: [{ name: '', box: [0.5, 0.1, 0.1, 0.1], source: 'MWG', ids: ['mwg:1'], key: 'mwg:1' }], unnamed_count: 1, has_positions: true, warnings: [] }
    server.faces = faces
    server.fields = { keywords: ['Ann', 'picnic', 'DATE: Y~'] }
    const { app, s } = await open()
    app.updateFace(s, 'mwg:0', { name: 'Anne' })
    expect(s.draft.meta!.faces!['mwg:0']).toEqual({ was: { name: 'Ann', box: [0.1, 0.1, 0.1, 0.1] }, name: 'Anne' })
    expect(s.draft.meta!.keywords).toEqual(['picnic', 'Anne'])
    app.updateFace(s, 'mwg:0', { name: 'Ann' })
    expect(s.draft.meta).toBeUndefined()                   // back to the file: no edits at all
    app.updateFace(s, 'mwg:1', { name: 'Bob' })
    expect(app.faces(s).named.map((f: any) => f.name)).toEqual(['Ann', 'Bob'])
    expect(s.draft.meta!.keywords).toEqual(['Ann', 'picnic', 'Bob'])
    const k = app.addFace(s, [0.7, 0.7, 0.1, 0.1], 'Cy')
    expect(k.startsWith('new:')).toBe(true)
    app.deleteFace(s, 'mwg:0')
    expect(app.faces(s).named.map((f: any) => f.name)).toEqual(['Bob', 'Cy'])
    expect(s.draft.meta!.keywords).toEqual(['picnic', 'Bob', 'Cy'])
    app.undo(s)
    expect(app.faces(s).named.map((f: any) => f.name)).toEqual(['Ann', 'Bob', 'Cy'])   // one undo step each
  })

  it('a draft for another version of the file keeps details, and face edits only where the face is found', async () => {
    const { draftForFile } = await import('../lib/store.svelte')
    const meta: any = { info: { upright_width: 1000, upright_height: 800 }, faces: {
      named: [{ name: 'Ann', box: [0.1, 0.1, 0.1, 0.1], source: 'MWG', ids: ['mwg:2'], key: 'mwg:2' }], unnamed: [] } }
    const stored = { templateId: 'tpl', overrides: {}, blocks: {}, mode: 'erase', _stat: [9, '9'], _size: [1000, 800],
      meta: { title: 'T', faces: { 'mwg:0': { name: 'Anne', was: { name: 'Ann', box: [0.1, 0.1, 0.1, 0.1] } },
        'mwg:5': { deleted: true, was: { name: 'Gone', box: [0.5, 0.5, 0.1, 0.1] } }, 'new:x': { name: 'Bo', box: [0.3, 0.3, 0.1, 0.1] } } } }
    const base: any = { templateId: 'tpl', overrides: {}, blocks: {}, mode: 'band', sourceRect: null, photoRect: null }
    const r = draftForFile(stored, [1, '1'], base, () => true, meta)
    expect(r.stale).toBe(true)
    expect(r.draft.mode).toBe('band')
    expect(r.draft.meta!.title).toBe('T')
    expect(Object.keys(r.draft.meta!.faces!).sort()).toEqual(['mwg:2', 'new:x'])   // Ann found under her new key
    expect(r.droppedFaces).toBe(1)
    const other = draftForFile({ ...stored, _size: [800, 1000] }, [1, '1'], base, () => true, meta)
    expect(other.draft.meta!.faces).toBeUndefined()
    expect(other.draft.meta!.title).toBe('T')
  })
})

