<script lang="ts">
  import { onDestroy, untrack } from 'svelte'
  import { download, get, post, postForm } from '../lib/api'
  import { dialogs } from '../lib/dialogs.svelte'
  import { app, draftForFile, PhotoSession, type PhotoItem } from '../lib/store.svelte'
  import { caseCOverwriteRefused, planCaseC } from '../lib/batchplan'
  import type { ExistingAnalysis, PhotoMeta, Settings } from '../lib/types'
  import Disclosure from './Disclosure.svelte'
  import Icon from './Icon.svelte'
  import Segmented from './Segmented.svelte'

  type Plan = {
    path: string
    name: string
    status: 'pending' | 'ready' | 'flagged' | 'skipped' | 'blocked' | 'excluded' | 'error'
    reasons: string[]
    action: 'band' | 'rebuild' | 'erase' | 'skip'
    session?: PhotoSession
    reviewed?: boolean
    usesDraft?: boolean
    onCopy?: boolean // case C in an overwrite batch: the server erases on a copy and keeps the original
    draftHash?: string | null // the stored draft this plan started from (cleared after the batch saves it)
    size?: number
    before?: { status: Plan['status']; reasons: string[] } // state before "Exclude" in review
  }
  /** What the batch keeps of a plan (so a resume can prepare unstaged photos again). */
  type KeptPlan = { index: number | null; path: string; name: string; status: string; action: string; reasons: string[]; usesDraft: boolean; edited: boolean }
  type BatchSettings = Settings['batch']

  let step = $state<'setup' | 'preflight' | 'running' | 'summary'>('setup')
  let folder = $state('')
  let files = $state<{ path: string; name: string; size: number }[]>([])
  let selected = $state<Set<string>>(new Set())
  let templateId = $state(app.defaultTemplateId())
  const B = $derived(app.settings.batch)
  let plans = $state<Plan[]>([])
  let pfDone = $state(0)
  let pfTotal = $state(0)
  let pfId = ''
  let pfLive = false // a pre-flight of this view is still running on the server
  let batchId = $state('')
  let summary = $state<any>(null)
  let staged = $state(0)
  let stageTotal = $state(0)
  let stagingBusy = $state(false)
  let poll: any
  let cancelled = $state(false)
  let whichNote = $state('')
  let pfGen = 0

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

  function setB(patch: Partial<Settings['batch']>) {
    app.saveSettings({ batch: patch })
  }

  async function chooseFolder() {
    const p = await dialogs.pick('open-folder', 'Choose a folder to caption', app.settings.session.lastFolder)
    if (!p?.length) return
    folder = p[0]
    await scan()
  }
  async function scan() {
    if (!folder) return
    try {
      files = await post('/api/batch/scan', { folder, includeSubfolders: B.includeSubfolders })
      selected = new Set(files.map((f) => f.path))
      whichNote = ''
      plans = []
    } catch (e: any) {
      app.toast('error', e.message)
    }
  }

  // Ticking boxes only means something in "Only selected" (and "No caption yet"), so
  // unticking a photo while "All" is chosen switches to "Only selected".
  function setSelected(next: Set<string>) {
    selected = next
    if (B.which === 'all' && next.size < files.length) {
      setB({ which: 'selected' })
      whichNote = 'Switched to “Only selected” because you unticked a photo.'
    }
  }
  function toggleFile(path: string, on: boolean) {
    const s = new Set(selected)
    if (on) s.add(path)
    else s.delete(path)
    setSelected(s)
  }
  function setWhich(v: string) {
    whichNote = ''
    if (v === 'all') selected = new Set(files.map((f) => f.path))
    setB({ which: v as any })
  }

  // -------------------------------------------------------------- pre-flight
  function stopPreflight() {
    if (pfId && pfLive) post(`/api/batch/preflight/${pfId}/cancel`).catch(() => {})
    pfLive = false
    pfGen++
  }

  /** Runs a server pre-flight over `paths` and hands each result to `onResult` (in order of arrival).
   *  Polling survives transient errors; after repeated failures the rest is reported as errors. */
  async function runPreflight(paths: string[], ocr: boolean, onResult: (res: any) => Promise<void>, isStopped: () => boolean) {
    const r = await post('/api/batch/preflight', { paths, ocr, replaces: pfLive ? pfId : undefined })
    pfId = r.id
    pfLive = true
    const my = pfId
    let since = 0
    let fails = 0
    const seen = new Set<string>()
    while (!isStopped()) {
      let st: any
      try {
        st = await get(`/api/batch/preflight/${my}`, { since })
        fails = 0
      } catch (e: any) {
        if (++fails >= 12 || e?.status === 404) {
          for (const p of paths) if (!seen.has(p)) await onResult({ path: p, ok: false, error: `Checking stopped: ${e.message}` })
          break
        }
        await sleep(Math.min(3000, 350 * 2 ** fails))
        continue
      }
      since += st.results.length
      for (const res of st.results) {
        seen.add(res.path)
        await onResult(res)
      }
      if (st.done >= st.total) break
      await sleep(350)
    }
    if (pfId === my) pfLive = false
  }

  async function preflight() {
    const t = app.template(templateId)
    if (!t) return
    let list = files
    if (B.which !== 'all') list = files.filter((f) => selected.has(f.path))
    if (!list.length) {
      app.toast('warn', 'No photos selected.')
      return
    }
    step = 'preflight'
    cancelled = false
    plans = list.map((f) => ({ path: f.path, name: f.name, size: f.size, status: 'pending', reasons: [], action: 'band' }))
    pfDone = 0
    pfTotal = list.length
    const needOcr = B.caseB !== 'skip' || B.caseC !== 'skip'
    const gen = ++pfGen
    try {
      await runPreflight(list.map((f) => f.path), needOcr, async (res) => {
        if (gen !== pfGen) return
        await planFor(res)
        pfDone = plans.filter((p) => p.status !== 'pending').length
      }, () => cancelled || gen !== pfGen)
    } catch (e: any) {
      if (gen !== pfGen) return
      app.toast('error', `Checking the photos failed: ${e.message}`)
      plans = plans.map((p) => (p.status === 'pending' ? { ...p, status: 'error', reasons: [`Checking failed: ${e.message}`] } : p))
      pfDone = pfTotal
    }
  }

  /** Readable message for a photo that could not be read. */
  function readError(msg: string, size?: number): string {
    if (size === 0 || /zero[- ]byte|empty file|file is empty/i.test(msg || '')) return 'Empty file'
    if (/unsupported file type|cannot identify|not a (valid|supported)|truncated|corrupt|damaged|unexpected end|premature end|decod|invalid (tiff|jpeg|png)|not an image/i.test(msg || '')) {
      return 'Can’t read this file (damaged or not an image)'
    }
    return msg || 'Can’t read this file'
  }

  /** "{names}" only inside an optional [...] group: an empty value is expected, not a problem. */
  function namesOptional(fmt: string): boolean {
    let depth = 0
    let any = false
    for (let i = 0; i < fmt.length; i++) {
      const c = fmt[i]
      if (c === '\\') { i++; continue }
      if (c === '[') depth++
      else if (c === ']') depth = Math.max(0, depth - 1)
      else if (c === '{' && fmt.startsWith('{names', i)) {
        any = true
        if (depth === 0) return false
      }
    }
    return any
  }

  async function planFor(res: any) {
    const i = plans.findIndex((p) => p.path === res.path)
    if (i < 0) return
    let p: Plan
    try {
      p = await computePlan(res, plans[i], B, templateId)
    } catch (e: any) {
      p = { ...plans[i], status: 'error', reasons: [`Could not plan this photo: ${e?.message || e}`] }
    }
    const k = plans.findIndex((x) => x.path === res.path)
    if (k >= 0) plans[k] = p
  }

  /** The same planning for pre-flight and for preparing photos again on resume/retry. */
  async function computePlan(res: any, base: Plan, bs: BatchSettings, tid: string): Promise<Plan> {
    const p: Plan = { ...base, reasons: [], usesDraft: false, onCopy: false, draftHash: null }
    if (!res.ok) {
      p.status = 'error'
      p.reasons = [readError(res.error, base.size)]
      return p
    }
    if (res.blocked) {
      p.status = 'blocked'
      p.reasons = [res.blocked === 'Multi-page TIFF' ? 'Multi-page TIFF — saving would drop the extra pages (can be allowed in Settings › Saving)' : res.blocked]
      return p
    }
    const meta: PhotoMeta = res.meta
    const ex: ExistingAnalysis = res.existing
    const s = new PhotoSession(p.path)
    ;(s as any).__batch = true // never autosaved as a single-photo draft
    s.meta = meta
    s.existing = ex
    const draft: any = bs.useDrafts ? await get('/api/photo/meta', { path: p.path }).then((m) => m.draft).catch(() => null) : null
    s.draft = { templateId: tid, overrides: {}, blocks: {}, mode: 'band', sourceRect: null, photoRect: null }
    const reasons: string[] = []
    let action: Plan['action'] = 'band'
    if (ex?.case === 'A') {
      if (bs.caseA === 'skip') action = 'skip'
      else {
        action = 'rebuild'
        s.draft.sourceRect = ex.sourceRect!
      }
      if (bs.which === 'uncaptioned') action = 'skip'
    } else if (ex?.case === 'B') {
      if (bs.caseB === 'skip' || bs.which === 'uncaptioned') action = 'skip'
      else {
        action = 'rebuild'
        s.draft.sourceRect = ex.band!.photo_rect
        s.draft.photoRect = ex.band!.photo_rect
        if ((ex.confidence ?? 1) < 0.6) reasons.push('not sure where the existing caption is')
      }
    } else if (ex?.case === 'C') {
      // as the server does it: in an overwrite batch the caption is erased on a copy, the original is kept
      const c = planCaseC(bs)
      action = c.action
      p.onCopy = c.onCopy
      if (action === 'erase') {
        s.draft.mode = 'erase'
        s.draft.photoRect = ex.band!.photo_rect
        s.draft.existingChoice = 'template'
      }
    } else if (ex?.case === 'D') {
      reasons.push('text printed over the photo (left untouched)')
    }
    if (action === 'rebuild') s.draft.mode = 'rebuild'
    if (draft && action !== 'skip') {
      // the editor's rule: a draft made for another version of the file gives back only its text
      // and style; the mode and photo edges stay as planned here for this version
      const d = draftForFile(draft, meta.stat, s.draft, (id) => !!app.template(id))
      s.draft = d.draft
      p.usesDraft = true
      p.draftHash = d.hash
      if (ex?.case === 'C') {
        p.onCopy = bs.saveMode === 'overwrite' && s.draft.mode === 'erase'
        const refused = caseCOverwriteRefused(s.draft.mode, bs, !!app.settings.saving.allowOverwriteHandwritten)
        if (refused) {
          action = 'skip'
          reasons.push(refused)
        }
      }
    }
    p.action = action
    p.session = s
    if (action === 'skip') {
      p.status = 'skipped'
      p.reasons = reasons.length ? reasons : [ex?.case === 'A' ? 'already captioned by Photoband' : 'has an existing caption']
      return p
    }
    await app.resolveAll(s)
    await app.relayout(s)
    for (const w of app.warnings(s)) {
      if (!['overflow', 'glyph', 'metadata'].includes(w.kind)) continue
      // missing names are judged below per block; unknown name order is informational
      if (w.code === 'names-missing' || w.code === 'names-order') continue
      reasons.push(w.message)
    }
    const eff = app.effective(s)
    const namesMissing = (eff?.blocks || []).some((b) => {
      if (!b.format.includes('{names')) return false
      const r = s.resolved[b.id]
      if (!r || !(r.empty_tokens || []).some((t) => t.startsWith('names'))) return false
      return !app.blockText(s, b.id).trim() && !namesOptional(b.format)
    })
    if (namesMissing) reasons.push('no names found')
    if (ex?.blocks?.some((b) => b.lines.some((l) => l.confidence < 0.6))) reasons.push('some existing caption text was hard to read')
    p.reasons = [...new Set(reasons)]
    p.status = p.reasons.length ? 'flagged' : 'ready'
    return p
  }

  const counts = $derived.by(() => {
    const c: Record<string, number> = {}
    for (const p of plans) c[p.status] = (c[p.status] || 0) + 1
    return c
  })
  // planFor is async: pfDone can reach the total before the last rows are planned
  const pfReady = $derived(pfDone >= pfTotal && plans.every((p) => p.status !== 'pending'))
  const toSave = $derived(plans.filter((p) => p.status === 'ready' || (p.status === 'flagged' && (B.warnings === 'include' || p.reviewed))))
  const heldBack = $derived(plans.filter((p) => p.status === 'flagged' && B.warnings === 'hold' && !p.reviewed))

  // -------------------------------------------------------------- review (uses the full editor)
  // The editor's own photos are put aside while reviewing and restored afterwards.
  let viewed = new Set<string>()
  let stash: { photos: PhotoItem[]; sessions: [string, PhotoSession][]; current: number } | null = null
  $effect(() => {
    const p = app.batchReview ? app.session?.path : null
    if (p) viewed.add(p)
  })
  const EXCLUDED_MSG = 'Excluded from this batch; it won’t be saved. Press Exclude again to include it.'

  function restoreEditor() {
    const st = stash
    stash = null
    app.closeAll()
    if (!st) return
    app.photos = st.photos
    for (const [k, v] of st.sessions) app.sessions.set(k, v)
    if (st.current >= 0 && st.current < st.photos.length) app.select(st.current, true)
  }

  function review(which: 'all' | 'flagged') {
    const list = plans.filter((p) => (which === 'all' ? p.status === 'ready' || p.status === 'flagged' : p.status === 'flagged'))
    if (!list.length) {
      app.toast('info', which === 'flagged' ? 'Nothing is flagged.' : 'Nothing to review.')
      return
    }
    const inList = new Set(list.map((p) => p.path))
    stash = { photos: $state.snapshot(app.photos) as PhotoItem[], sessions: [...app.sessions.entries()], current: app.current }
    viewed = new Set()
    // load the batch sessions into the editor
    app.closeAll()
    for (const p of list) if (p.session) app.sessions.set(p.path, p.session)
    // "untouched", not "draft": nothing here is an unsaved single-photo edit
    app.photos = list.map((p) => ({ path: p.path, name: p.name, status: 'untouched' }))
    app.batchReview = {
      paths: list.map((p) => p.path),
      onExit: async () => {
        const unseen = plans.filter((p) => inList.has(p.path) && p.status === 'flagged' && !viewed.has(p.path) && !p.reviewed)
        let includeUnseen = false
        if (unseen.length && B.warnings === 'hold') {
          const n = unseen.length
          const r = await dialogs.ask(
            'Some flagged photos weren’t opened',
            `${n} flagged photo${n > 1 ? 's were' : ' was'} not opened during this review:\n${unseen.slice(0, 6).map((p) => '• ' + p.name).join('\n')}${n > 6 ? `\n… and ${n - 6} more` : ''}\n\nHold ${n > 1 ? 'them' : 'it'} back, or include ${n > 1 ? 'them' : 'it'} in the batch anyway?`,
            [
              { id: 'cancel', label: 'Keep reviewing' },
              { id: 'include', label: 'Include anyway' },
              { id: 'hold', label: 'Hold back', kind: 'primary' },
            ],
          )
          if (r.id === 'cancel') return
          includeUnseen = r.id === 'include'
        }
        for (const p of plans) {
          if (!inList.has(p.path)) continue
          const s = app.sessions.get(p.path)
          if (s) {
            if (s.error === EXCLUDED_MSG) s.error = ''
            ;(s as any).__batch = true
            p.session = s
          }
          if (p.status === 'flagged' && (viewed.has(p.path) || includeUnseen || s?.dirty)) p.reviewed = true
        }
        plans = [...plans]
        app.batchReview = null
        ;(window as any).__batchExclude = undefined
        restoreEditor()
        app.view = 'batch'
      },
    }
    // Exclude toggles; returns true when the photo is now excluded.
    ;(window as any).__batchExclude = (path: string): boolean => {
      const i = plans.findIndex((x) => x.path === path)
      if (i < 0) return false
      const p = { ...plans[i] }
      const s = app.sessions.get(path)
      const k = app.photos.findIndex((x) => x.path === path)
      let excluded: boolean
      if (p.status === 'excluded') {
        p.status = p.before?.status ?? 'ready'
        p.reasons = p.before?.reasons ?? []
        p.before = undefined
        if (s && s.error === EXCLUDED_MSG) s.error = ''
        if (k >= 0) app.photos[k] = { ...app.photos[k], name: p.name, status: s?.dirty ? 'draft' : 'untouched', error: undefined }
        excluded = false
      } else {
        p.before = { status: p.status, reasons: p.reasons }
        p.status = 'excluded'
        p.reasons = ['excluded in review']
        if (s && !s.error) s.error = EXCLUDED_MSG
        if (k >= 0) app.photos[k] = { ...app.photos[k], name: `${p.name} (excluded)`, status: 'error', error: 'Excluded from this batch' }
        excluded = true
      }
      plans[i] = p
      plans = [...plans]
      // the review bar shows its own generic toast right after this call; replace it with one that can be undone
      setTimeout(() => {
        app.toasts = app.toasts.filter((t) => t.text !== 'Excluded from this batch.')
        const undo = { label: 'Undo', run: () => (window as any).__batchExclude?.(path) }
        if (excluded) app.toast('info', `Excluded ${p.name} from this batch.`, undo)
        else app.toast('info', `${p.name} is back in the batch.`)
      }, 0)
      if (excluded && app.session?.path === path && app.current < app.photos.length - 1) app.next()
      return excluded
    }
    app.view = 'editor'
    app.select(0, true)
  }

  // -------------------------------------------------------------- save all
  /** Lays out and stages one photo. False when it could not be prepared (journaled as failed). */
  async function stageOne(bid: string, index: number, p: Plan, mode: string): Promise<boolean> {
    try {
      const { job, tiles } = await app.buildJob(p.session!, mode === 'overwrite' ? 'overwrite' : 'copy')
      job.index = index
      // the stored draft this photo started from: the batch deletes it once the photo is saved
      job.draft_hash = p.usesDraft ? p.draftHash ?? null : null
      const form = new FormData()
      form.append('job', JSON.stringify(job))
      tiles.forEach((t, k) => form.append(`tile${k}`, t.blob, `tile${k}.png`))
      await postForm(`/api/batch/${bid}/stage`, form)
      return true
    } catch (e: any) {
      await post(`/api/batch/${bid}/exclude`, { index, path: p.path, state: 'failed', reason: `Could not prepare: ${e.message}` }).catch(() => {})
      return false
    }
  }

  async function markStopped(bid: string, items: { index: number; path: string }[]) {
    if (!items.length) return
    await post(`/api/batch/${bid}/exclude`, {
      items: items.map((x) => ({ index: x.index, path: x.path, state: 'cancelled', reason: 'Stopped before it was prepared' })),
    }).catch(() => {})
  }

  async function saveAll() {
    const n = toSave.length
    if (!n || !pfReady) return
    const bk = app.settings.saving.backupOriginals
    const msg = B.saveMode === 'overwrite'
      ? `Overwrite ${n} original${n > 1 ? 's' : ''}${bk ? ` (backups to ${app.settings.saving.backupFolder || '_originals'})` : ' WITHOUT backups'}?`
      : `Save ${n} captioned cop${n > 1 ? 'ies' : 'y'} (${app.settings.saving.location === 'fixed' ? app.settings.saving.fixedFolder : app.settings.saving.location === 'same' ? 'next to the originals' : `into “${app.settings.saving.subfolderName}” folders`})?`
    const copies = B.saveMode === 'overwrite' ? toSave.filter((p) => p.onCopy).length : 0
    const detail = (copies ? `\n\n${copies} photo${copies > 1 ? 's have' : ' has'} a handwritten or printed caption: erased on a copy, the original is kept.` : '') +
      (heldBack.length ? `\n\n${heldBack.length} flagged photo${heldBack.length > 1 ? 's are' : ' is'} held back for review.` : '')
    const r = await dialogs.ask('Save all', msg + detail, [{ id: 'cancel', label: 'Cancel' }, { id: 'ok', label: saveLabel(n), kind: B.saveMode === 'overwrite' ? 'danger' : 'primary' }])
    if (r.id !== 'ok') return
    const list = [...toSave]
    const idx = new Map(list.map((p, i) => [p.path, i]))
    // the whole plan: resume prepares unstaged photos again from it; the rest is journaled with reasons
    const kept: KeptPlan[] = plans.map((p) => ({
      index: idx.get(p.path) ?? null,
      path: p.path,
      name: p.name,
      status: p.status === 'flagged' && !idx.has(p.path) ? 'held' : p.status,
      action: p.action,
      reasons: p.status === 'flagged' && !idx.has(p.path) ? ['held back for review', ...p.reasons] : p.reasons,
      usesDraft: !!p.usesDraft,
      edited: !!p.session?.dirty,
    }))
    const b = await post('/api/batch/create', { files: list.map((p) => p.path), plan: kept, templateId, folder, batchSettings: $state.snapshot(B) })
    batchId = b.id
    const bid = b.id
    step = 'running'
    staged = 0
    stageTotal = list.length
    stagingBusy = true
    try {
      await post(`/api/batch/${bid}/run`)
      startPolling()
      for (let i = 0; i < list.length; i++) {
        if (cancelled) {
          await markStopped(bid, list.slice(i).map((p, k) => ({ index: i + k, path: p.path })))
          break
        }
        await stageOne(bid, i, list[i], B.saveMode)
        staged = i + 1
        await sleep(0)
      }
      await post(`/api/batch/${bid}/staging-complete`)
    } finally {
      stagingBusy = false
    }
  }

  /** Prepares batch entries again from the batch's kept pre-flight plan (photos never staged:
   *  the app quit while preparing them, the batch was stopped, or preparing failed). Photos
   *  that can't be prepared the same way are listed as "not prepared" with the reason. */
  async function restageFromPlan(bid: string, meta: any, indices: number[]) {
    if (!indices.length) return
    const bs: BatchSettings = { ...B, ...(meta?.settings || {}) }
    const tid = meta?.templateId && app.template(meta.templateId) ? meta.templateId : templateId
    const kept: KeptPlan[] = Array.isArray(meta?.plan) ? meta.plan : []
    const byIndex = new Map(kept.filter((x) => x.index !== null && x.index !== undefined).map((x) => [Number(x.index), x]))
    const notPrepared: { index: number; path: string; state: string; reason: string }[] = []
    const todo: KeptPlan[] = []
    for (const i of indices) {
      const it = byIndex.get(i)
      const path = it?.path || meta?.files?.[i] || ''
      if (!it) notPrepared.push({ index: i, path, state: 'notPrepared', reason: 'Not prepared before the batch stopped (its check results weren’t kept); run it again as a new batch' })
      else if (it.edited) notPrepared.push({ index: i, path, state: 'notPrepared', reason: 'Edited in review but not prepared before the batch stopped; run it again as a new batch' })
      else todo.push(it)
    }
    staged = 0
    stageTotal = todo.length
    if (todo.length) {
      const needOcr = bs.caseB !== 'skip' || bs.caseC !== 'skip'
      const byPath = new Map(todo.map((x) => [x.path, x]))
      const stopped: KeptPlan[] = []
      const gen = ++pfGen
      await runPreflight(todo.map((x) => x.path), needOcr, async (res) => {
        const it = byPath.get(res.path)
        if (!it) return
        byPath.delete(res.path)
        if (cancelled) {
          stopped.push(it)
          return
        }
        let np: Plan
        try {
          np = await computePlan(res, { path: it.path, name: it.name, status: 'pending', reasons: [], action: 'band' }, bs, tid)
        } catch (e: any) {
          np = { path: it.path, name: it.name, status: 'error', reasons: [String(e?.message || e)], action: 'band' }
        }
        if ((np.status === 'ready' || np.status === 'flagged') && np.action === it.action) {
          await stageOne(bid, it.index!, np, bs.saveMode)
        } else {
          const why = np.status === 'ready' || np.status === 'flagged' ? `now it would ${np.action === 'skip' ? 'be skipped' : np.action}` : `${labels[np.status] || np.status}${np.reasons.length ? ': ' + np.reasons.join('; ') : ''}`
          notPrepared.push({ index: it.index!, path: it.path, state: 'notPrepared', reason: `Changed since it was checked (${why})` })
        }
        staged++
      }, () => gen !== pfGen)
      for (const it of [...stopped, ...byPath.values()]) {
        if (cancelled) await markStopped(bid, [{ index: it.index!, path: it.path }])
        else notPrepared.push({ index: it.index!, path: it.path, state: 'notPrepared', reason: 'Could not be prepared again' })
      }
    }
    if (notPrepared.length) await post(`/api/batch/${bid}/exclude`, { items: notPrepared }).catch(() => {})
  }

  /** POST that retries a 409 (a run lock left by a process that just died) with backoff. */
  async function postRetry(url: string, tries = 7) {
    for (let a = 0; ; a++) {
      try {
        return await post(url)
      } catch (e: any) {
        if (e?.status !== 409 || a >= tries - 1) throw e
        await sleep(250 * 2 ** a)
      }
    }
  }

  function startPolling() {
    clearInterval(poll)
    poll = setInterval(async () => {
      try {
        summary = await get(`/api/batch/${batchId}`)
        if ((summary.state === 'finished' || summary.state === 'cancelled') && !stagingBusy) {
          clearInterval(poll)
          step = 'summary'
          app.incompleteBatches = app.incompleteBatches.filter((b) => b.id !== batchId)
        }
      } catch { /* keep polling */ }
    }, 500)
  }

  async function cancelRun() {
    cancelled = true
    await post(`/api/batch/${batchId}/cancel`)
    app.toast('info', 'Stopping after the photos in progress finish.')
  }
  async function retry() {
    const sm = summary
    step = 'running'
    cancelled = false
    stagingBusy = true
    try {
      // entries that never got a staged job (stopped or failed while preparing) are prepared again first
      const needs = (sm?.entries || []).filter((e: any) => (e.state === 'failed' || e.state === 'cancelled') && e.hasJob === false).map((e: any) => e.index)
      startPolling()
      await restageFromPlan(batchId, sm?.meta, needs)
      await postRetry(`/api/batch/${batchId}/retry`)
    } catch (e: any) {
      app.toast('error', `Could not retry: ${e.message}`)
    } finally {
      stagingBusy = false
    }
  }
  async function restore() {
    const n = running.done || 0
    const ok = await dialogs.confirm(
      'Restore originals',
      `Put back the original${n === 1 ? '' : 's'} of the ${n || ''} photo${n === 1 ? '' : 's'} this batch overwrote? Each captioned version is replaced by the backup kept before saving (in ${backupsWhere}).\n\nIf you edited a photo after the batch, you are asked about it separately.`,
      'Restore originals',
      true,
    )
    if (!ok) return
    let r = await post(`/api/batch/${batchId}/restore`, {})
    const results: any[] = r.results || []
    const edited = results.filter((x) => x.result === 'skipped-edited')
    if (edited.length) {
      const names = edited.slice(0, 5).map((x) => (x.path || '').split(/[\\/]/).pop()).join(', ')
      const again = await dialogs.ask('Some photos changed after the batch', `${edited.length} photo${edited.length > 1 ? 's were' : ' was'} edited after the batch (${names}${edited.length > 5 ? '…' : ''}). Restoring replaces those edits; the edited versions are kept next to the backups.`, [
        { id: 'skip', label: 'Leave them' },
        { id: 'force', label: 'Restore them too', kind: 'danger' },
      ])
      if (again.id === 'force') {
        const r2 = await post(`/api/batch/${batchId}/restore`, { force: true, indices: edited.map((x) => x.index) })
        r = { ...r, restored: (r.restored || 0) + (r2.restored || 0), errors: [...(r.errors || []), ...(r2.errors || [])] }
      }
    }
    const missing = results.filter((x) => x.result === 'missing-backup').length
    const errs = results.filter((x) => x.result === 'error').length
    app.toast(errs || missing ? 'warn' : 'success', `Restored ${r.restored} original${r.restored === 1 ? '' : 's'}.${missing ? ` ${missing} backup${missing > 1 ? 's were' : ' was'} missing.` : ''}${errs ? ` ${errs} failed.` : ''}`)
    summary = await get(`/api/batch/${batchId}`)
  }
  function newBatch() {
    step = 'setup'
    plans = []
    summary = null
    batchId = ''
    scan()
  }

  // "Resume batch" in the editor sets window.__photobandResume and switches to this view.
  // The view stays mounted once shown, so check the flag every time it becomes visible.
  async function resumeBatch(rid: string) {
    if (step === 'running') {
      app.toast('warn', 'Another batch is saving. Wait for it to finish or cancel it, then resume.')
      return
    }
    let sm: any
    try {
      sm = await post(`/api/batch/${rid}/resume`)
    } catch (e: any) {
      app.toast('error', `Could not resume the batch: ${e.message}`)
      if (e?.status === 404) app.incompleteBatches = app.incompleteBatches.filter((b) => b.id !== rid)
      return
    }
    try {
      batchId = rid
      plans = []
      staged = 0
      stageTotal = 0
      cancelled = false
      summary = sm
      step = 'running'
      stagingBusy = true
      startPolling()
      // photos the app never staged (it quit before or while preparing them): prepare them again
      const count = Number(sm.meta?.count ?? sm.meta?.files?.length ?? sm.expected ?? 0)
      const have = new Set((sm.entries || []).map((e: any) => Number(e.index)))
      const missing: number[] = []
      for (let i = 0; i < count; i++) if (!have.has(i)) missing.push(i)
      if (missing.length) {
        app.toast('info', `Preparing ${missing.length} photo${missing.length > 1 ? 's' : ''} that weren’t ready when the batch stopped…`)
        await restageFromPlan(rid, sm.meta, missing)
      }
      if (!cancelled) await post(`/api/batch/${rid}/staging-complete`)
      await postRetry(`/api/batch/${rid}/run`)
    } catch (e: any) {
      app.toast('error', `Could not resume the batch: ${e.message}`)
    } finally {
      stagingBusy = false
    }
  }
  function checkResume() {
    const rid = (window as any).__photobandResume
    if (!rid) return
    ;(window as any).__photobandResume = null
    resumeBatch(rid)
  }
  $effect(() => {
    if (app.view !== 'batch') return
    untrack(checkResume)
    // App sets the flag right after switching the view; catch that on the next tick too
    const t = setTimeout(checkResume, 0)
    return () => clearTimeout(t)
  })
  onDestroy(() => {
    clearInterval(poll)
    stopPreflight()
  })

  const running = $derived(summary?.counts ?? {})
  const skippedN = $derived((running.changed || 0) + (running.skipped || 0) + (running.excluded || 0))
  const notSavedN = $derived((running.held || 0) + (running.blocked || 0) + (running.notPrepared || 0))
  const doneN = $derived((running.done || 0) + (running.failed || 0) + skippedN + notSavedN + (running.restored || 0))
  const total = $derived(summary?.expected || toSave.length || 1)
  const retryLabel = $derived(
    running.failed && running.cancelled ? 'Retry failed & resume stopped' : running.failed ? 'Retry failed' : running.cancelled ? 'Resume stopped photos' : '',
  )
  function rel(p: string): string {
    const base = (summary?.meta?.folder || folder || '').replace(/[\\/]+$/, '')
    if (base && p.startsWith(base) && /[\\/]/.test(p.charAt(base.length))) return p.slice(base.length + 1)
    return p.split(/[\\/]/).pop() || p
  }
  const labels: Record<string, string> = { ready: 'Ready', flagged: 'Needs a look', skipped: 'Skipped', blocked: 'Blocked', excluded: 'Excluded', error: 'Error', pending: 'Checking…' }
  const entryLabels: Record<string, string> = { staged: 'Queued', retry: 'Queued', running: 'Saving', done: 'Saved', failed: 'Failed', changed: 'Changed', skipped: 'Skipped', excluded: 'Excluded', cancelled: 'Stopped', restored: 'Restored', held: 'Held back', blocked: 'Blocked', notPrepared: 'Not prepared' }
  const entryPill: Record<string, string> = { done: 'ready', failed: 'error', running: 'pending', restored: 'restored', cancelled: 'skipped', changed: 'flagged', held: 'flagged', blocked: 'blocked', notPrepared: 'error' }
  /** "Save 6 copies" / "Overwrite 6 photos" */
  function saveLabel(n: number): string {
    return B.saveMode === 'overwrite' ? `Overwrite ${n} photo${n === 1 ? '' : 's'}` : `Save ${n} cop${n === 1 ? 'y' : 'ies'}`
  }
  // Three steps for people: choose the photos, check them, save them (saving ends in a summary).
  const steps = [
    ['choose', 'Choose photos', 'Pick a folder and how the photos are captioned and saved.'],
    ['check', 'Check', 'Photoband looks at every photo first and lists anything that needs a look.'],
    ['save', 'Save', 'Saves the photos and tells you what happened.'],
  ] as const
  const stepIdx = $derived(step === 'setup' ? 0 : step === 'preflight' ? 1 : 2)
  const finished = $derived(step === 'summary')

  let filesOpen = $state<boolean | undefined>(undefined)
  let detailsOpen = $state<boolean | undefined>(undefined)
  const folderName = $derived(folder.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || folder)
  const pickedN = $derived(B.which === 'all' ? files.length : selected.size)
  const SV = $derived(app.settings.saving)
  /** Where copies go, in words. */
  const copiesWhere = $derived(
    SV.location === 'fixed' ? (SV.fixedFolder ? `into ${SV.fixedFolder}` : 'into the folder chosen in Settings') : SV.location === 'same' ? 'next to each original' : `into a “${SV.subfolderName}” folder next to each original`,
  )
  const backupsWhere = $derived(SV.backupFolder || '“_originals” folders next to the photos')
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

  function pickWhich(v: string) {
    setWhich(v)
    if (v === 'selected') filesOpen = true
  }

  const checkLines = $derived.by(() => {
    if (!pfReady) return []
    const out: string[] = []
    const ready = counts.ready || 0
    const fl = counts.flagged || 0
    out.push(ready ? `${plural(ready, 'photo is', 'photos are')} ready to save.` : 'No photos are ready to save yet.')
    if (fl) {
      out.push(B.warnings === 'hold'
        ? `${plural(fl, 'photo needs', 'photos need')} a look (the reason is listed). ${fl === 1 ? 'It is' : 'They are'} held back unless you review ${fl === 1 ? 'it' : 'them'}.`
        : `${plural(fl, 'photo has', 'photos have')} a warning but will be saved anyway.`)
    }
    if (counts.skipped) out.push(`${plural(counts.skipped, 'photo is', 'photos are')} skipped, as set in More options.`)
    const bad = (counts.blocked || 0) + (counts.error || 0)
    if (bad) out.push(`${plural(bad, 'photo', 'photos')} can’t be saved by Photoband.`)
    return out
  })

  const outcome = $derived.by(() => {
    if (!finished || !summary) return { title: '', lines: [] as string[], tone: 'ok' }
    const done = running.done || 0
    const mode = summary?.meta?.settings?.saveMode || B.saveMode
    const lines: string[] = []
    if (running.restored) lines.push(`${plural(running.restored, 'original was', 'originals were')} put back from the backups. The captioned versions were removed.`)
    if (done) {
      if (mode === 'overwrite') lines.push(`${plural(done, 'original was', 'originals were')} replaced by ${done === 1 ? 'its' : 'their'} captioned version${SV.backupOriginals ? `. Backups of the originals are in ${backupsWhere}` : ''}.`)
      else lines.push(`${plural(done, 'captioned copy was', 'captioned copies were')} saved ${copiesWhere}. Your originals were not changed.`)
    }
    if (running.failed) lines.push(`${plural(running.failed, 'photo', 'photos')} couldn’t be saved; the list below says why. “Retry failed” tries again.`)
    if (running.cancelled) lines.push(`${plural(running.cancelled, 'photo was', 'photos were')} not saved because the batch was stopped. “Resume” saves ${running.cancelled === 1 ? 'it' : 'them'}.`)
    if (running.held) lines.push(`${plural(running.held, 'photo was', 'photos were')} held back because something needs a look, for example no names were found. Open ${running.held === 1 ? 'it' : 'them'} in the editor, or start a new batch and review ${running.held === 1 ? 'it' : 'them'}.`)
    if (running.blocked || running.notPrepared) lines.push(`${plural((running.blocked || 0) + (running.notPrepared || 0), 'photo', 'photos')} can’t be saved this way; the list below says why.`)
    if (skippedN) lines.push(`${plural(skippedN, 'photo was', 'photos were')} skipped (for example, already captioned, excluded, or changed during the batch).`)
    const stopped = summary.state === 'cancelled'
    const title = stopped ? `Stopped: ${plural(done, 'photo', 'photos')} saved` : running.restored && !done ? 'Originals restored' : done ? `${plural(done, 'photo', 'photos')} saved` : 'Nothing was saved'
    const tone = running.failed || stopped ? 'warn' : done || running.restored ? 'ok' : 'warn'
    return { title, lines, tone }
  })
  // problems are never hidden behind a closed disclosure
  $effect(() => {
    if (finished && (running.failed || running.cancelled || running.notPrepared)) detailsOpen = true
  })

  async function showInFolder() {
    const first = (summary?.entries || []).find((e: any) => e.state === 'done' && e.out)
    if (!first) return
    try {
      await post('/api/open-folder', { which: 'path', path: first.out })
    } catch (e: any) {
      app.toast('error', `Couldn’t show the folder: ${e.message}`)
    }
  }
  const pillTips: Record<string, string> = {
    ready: 'Ready to save.',
    flagged: 'Something may need fixing, such as a caption that doesn’t fit or a missing name. Review it in the editor.',
    skipped: 'Left as it is, as set in More options.',
    blocked: 'Photoband can’t save this file.',
    excluded: 'You excluded it during review.',
    error: 'This file couldn’t be read.',
    pending: 'Being checked…',
  }
</script>


<div class="batch" data-step={finished ? 'summary' : steps[stepIdx][0]}>
  <div class="steps row">
    <ol class="stepper" aria-label="Batch steps">
      {#each steps as [id, label, tip], i}
        {@const past = i < stepIdx || (finished && i === stepIdx)}
        {#if i}<li class="line" class:done={i <= stepIdx} aria-hidden="true"></li>{/if}
        <li class="step" class:on={i === stepIdx && !finished} class:past data-tip={tip} aria-current={i === stepIdx ? 'step' : undefined}>
          <span class="num">{#if past}<Icon name="check" size={12} stroke={2.5} />{:else}{i + 1}{/if}</span>
          <span>{label}</span>
          <span class="sr-only">{past ? '(done)' : i === stepIdx ? '(current step)' : ''}</span>
        </li>
      {/each}
    </ol>
    <span class="grow"></span>
    <button class="btn sm ghost" data-tip={step === 'running' ? 'The batch keeps saving while you are in the editor.' : 'Go back to captioning one photo at a time. This batch stays as it is.'} onclick={() => (app.view = 'editor')}>Back to editor</button>
  </div>

  <div class="body scroll">
    {#if step === 'setup'}
      <div class="stack">
        <section class="card">
          <h3>Photos</h3>
          {#if !folder}
            <div class="empty">
              <span class="big" aria-hidden="true"><Icon name="folder" size={28} /></span>
              <p>Choose a folder and Photoband adds a caption to every photo in it, using one template. You check them before anything is saved.</p>
              <button class="btn primary" data-tip="Pick the folder with the photos to caption." onclick={chooseFolder}><Icon name="folder" /> Choose folder…</button>
            </div>
          {:else}
            <div class="row folder">
              <span class="ficon" aria-hidden="true"><Icon name="folder" size={18} /></span>
              <div class="grow">
                <div class="fname" data-tip={folder}>{folderName}</div>
                <div class="faint small">{plural(files.length, 'photo', 'photos')}{B.includeSubfolders ? ', including subfolders' : ''}</div>
              </div>
              <button class="btn sm" data-tip="Pick a different folder." onclick={chooseFolder}>Change…</button>
            </div>
          {/if}
          <label class="row chk" data-tip="Also caption the photos in folders inside this one."><input type="checkbox" checked={B.includeSubfolders} onchange={(e) => { setB({ includeSubfolders: (e.target as HTMLInputElement).checked }); setTimeout(scan, 50) }} /> Include subfolders</label>
          {#if files.length}
            <div class="set">
              <span class="k">Caption</span>
              <div class="v">
                <Segmented label="Which files" value={B.which} options={[{ value: 'all', label: 'All photos', title: 'Caption every photo in the folder.' }, { value: 'selected', label: 'Only ticked', title: 'Caption only the photos ticked in the list below.' }, { value: 'uncaptioned', label: 'Only uncaptioned', title: 'Skip photos that already have a caption band.' }]} onchange={pickWhich} />
                {#if whichNote}<span class="note small" role="status">{whichNote}</span>{:else if B.which === 'uncaptioned'}<span class="faint small">Of the {selected.size} ticked photos, only those without a caption yet.</span>{/if}
              </div>
            </div>
            <Disclosure id="batch.files" label="Photo list" count={`(${pickedN} of ${files.length} ticked)`} tip="See the photos and tick only the ones you want." bind:open={filesOpen}>
              <div class="row small"><span class="grow"></span>
                <button class="btn sm ghost" data-tip="Tick every photo." onclick={() => (selected = new Set(files.map((f) => f.path)))}>Tick all</button>
                <button class="btn sm ghost" data-tip="Untick every photo, then tick the ones you want." onclick={() => setSelected(new Set())}>Untick all</button>
              </div>
              <div class="files scroll">
                {#each files as f (f.path)}
                  <label class="frow row"><input type="checkbox" checked={B.which === 'all' || selected.has(f.path)} onchange={(e) => toggleFile(f.path, (e.target as HTMLInputElement).checked)} /> <span class="grow name" data-tip={f.path}>{f.name}</span><span class="faint small">{(f.size / 1048576).toFixed(1)} MB</span></label>
                {/each}
              </div>
            </Disclosure>
          {/if}
        </section>

        <section class="card">
          <h3>Caption and save</h3>
          <div class="set">
            <span class="k">Template</span>
            <div class="v">
              <div class="row full">
                <select class="field grow" bind:value={templateId} aria-label="Batch template" data-tip="The caption look and wording used for every photo in this batch.">
                  {#each app.templates as t}<option value={t.id}>{t.name}</option>{/each}
                </select>
                <button class="btn" data-tip="Change what the caption says (opens Settings › Caption formats)." onclick={() => (dialogs.settingsOpen = 'formats')}><Icon name="pen" size={14} /> Edit formats…</button>
              </div>
            </div>
          </div>
          <div class="set">
            <span class="k">Save as</span>
            <div class="v">
              <Segmented label="Save mode" value={B.saveMode} options={[{ value: 'copy', label: 'Captioned copies', title: 'Keep the originals as they are and save captioned copies.' }, { value: 'overwrite', label: 'Overwrite originals', title: 'Replace each original with its captioned version (a backup is kept if backups are on).' }]} onchange={(v) => setB({ saveMode: v as any })} />
              {#if B.saveMode === 'copy'}
                <span class="faint small">Copies go {copiesWhere}. <button class="link" data-tip="Change where copies go and how they are named." onclick={() => (dialogs.settingsOpen = 'saving')}>Change…</button></span>
              {:else if SV.backupOriginals}
                <span class="faint small">Each original is backed up first (to {backupsWhere}), so you can restore it from the summary.</span>
              {:else}
                <span class="note warn small" role="alert">Backups are off, so overwritten originals can’t be restored. <button class="link" data-tip="Turn backups on in Settings › Saving." onclick={() => (dialogs.settingsOpen = 'saving')}>Turn backups on…</button></span>
              {/if}
            </div>
          </div>
          <Disclosure id="batch.more" label="More options" tip="Existing captions, saved edits, warnings and name clashes.">
            <div class="form">
              <span>Photos with warnings<small class="help">For example a caption that doesn’t fit or a missing name</small></span>
              <Segmented label="Photos with warnings" value={B.warnings} options={[{ value: 'hold', label: 'Hold back for review', title: 'Recommended: don’t save these until you have looked at them.' }, { value: 'include', label: 'Save anyway', title: 'Save them with the rest, warnings and all.' }]} onchange={(v) => setB({ warnings: v as any })} />
              <span>Your edits<small class="help">Unsaved edits you made to a photo in the editor</small></span>
              <Segmented label="Per-photo drafts" value={B.useDrafts ? 'yes' : 'no'} options={[{ value: 'yes', label: 'Use my edits', title: 'Where you already edited a photo’s caption in the editor, use that text.' }, { value: 'no', label: 'Template for all', title: 'Ignore earlier edits and caption every photo from the template.' }]} onchange={(v) => setB({ useDrafts: v === 'yes' })} />
              <span>Made by Photoband<small class="help">Captions this app made earlier</small></span>
              <Segmented label="Captions made by Photoband" value={B.caseA} options={[{ value: 'recaption', label: 'Re-caption', title: 'Replace the old Photoband band with a new one.' }, { value: 'skip', label: 'Skip', title: 'Leave these photos as they are.' }]} onchange={(v) => setB({ caseA: v as any })} />
              <span>Other bands<small class="help">Caption bands added by another app</small></span>
              <Segmented label="Caption bands from other apps" value={B.caseB} options={[{ value: 'rebuild', label: 'Rebuild band', title: 'Cut off the old band and add a new one. Its text is read (OCR) to start the caption.' }, { value: 'skip', label: 'Skip', title: 'Leave these photos as they are.' }]} onchange={(v) => setB({ caseB: v as any })} />
              <span>Physical captions<small class="help">Handwritten or printed captions on the scan</small></span>
              <div class="col">
                <Segmented label="Physical captions" value={B.caseC} options={[{ value: 'skip', label: 'Skip', title: 'Leave the handwriting alone (recommended for originals).' }, { value: 'erase', label: 'Erase in place', title: 'Erase the writing on a copy and print the caption in the same spot.' }]} onchange={(v) => setB({ caseC: v as any })} />
                <span class="faint small">Erasing handwriting on a scan is only done on copies{B.saveMode === 'overwrite' ? ': these originals are kept' : ''}.</span>
              </div>
              <span>Name already taken<small class="help">When a copy with the same name exists</small></span>
              <Segmented label="If the name exists" value={SV.onExists} options={[{ value: 'increment', label: 'Add -2, -3…', title: 'Keep both: the new copy gets a number added to its name.' }, { value: 'ask', label: 'Ask', title: 'In a batch, asking is not possible, so a number is added.' }, { value: 'overwrite', label: 'Replace', title: 'Replace an earlier copy of the same photo. Other files with that name are never replaced.' }]} onchange={(v) => app.saveSettings({ saving: { onExists: v } }).catch((e) => app.toast('error', e.message))} />
            </div>
            <p class="faint small">Photos are saved several at a time, and each one is checked after it is written. Reading the text of existing captions (OCR) only happens for the options that need it.</p>
          </Disclosure>
        </section>
      </div>
    {:else if step === 'preflight'}
      <section class="card wide">
        <div class="row">
          <h3 class="grow" aria-live="polite">{!pfReady ? `Checking photos… ${pfDone} of ${pfTotal}` : `${plural(pfTotal, 'photo', 'photos')} checked`}</h3>
        </div>
        <div class="bar" class:indet={pfDone === 0 && pfTotal > 0} role="progressbar" aria-label="Checking progress" aria-valuemin={0} aria-valuemax={pfTotal} aria-valuenow={pfDone}><div style="width:{(pfDone / Math.max(1, pfTotal)) * 100}%"></div></div>
        {#if !pfReady}
          <p class="lead">Photoband reads each photo’s information and lays out its caption, without saving anything.</p>
        {:else}
          <ul class="lead lines">{#each checkLines as l}<li>{l}</li>{/each}</ul>
        {/if}
        <div class="stats row">
          <span class="stat ok" data-tip={pillTips.ready}><b>{counts.ready || 0}</b> ready</span>
          <span class="stat warn" data-tip={pillTips.flagged}><b>{counts.flagged || 0}</b> need a look</span>
          <span class="stat" data-tip={pillTips.skipped}><b>{counts.skipped || 0}</b> skipped</span>
          {#if counts.excluded}<span class="stat" data-tip={pillTips.excluded}><b>{counts.excluded}</b> excluded</span>{/if}
          {#if counts.blocked || counts.error}<span class="stat bad" data-tip="Photoband can’t save these files; the list says why."><b>{(counts.blocked || 0) + (counts.error || 0)}</b> can’t be saved</span>{/if}
        </div>
        <div class="plist scroll">
          {#each plans as p (p.path)}
            <div class="prow row">
              <span class="pill {p.status}" data-tip={pillTips[p.status]}>{labels[p.status]}</span>
              <span class="name" data-tip={p.path}>{p.name}</span>
              <span class="reasons" data-tip={[p.action !== 'band' && p.status !== 'skipped' ? (p.action === 'rebuild' ? 'Replaces the existing band.' : p.onCopy ? 'Erases the old caption on a copy; the original is kept.' : 'Erases the old caption in place.') : '', ...p.reasons].filter(Boolean).join('\n') || undefined}>{#if p.usesDraft && p.status !== 'skipped'}<span class="tag">uses your edits</span>{/if}{[p.action !== 'band' && p.status !== 'skipped' ? (p.action === 'rebuild' ? 'replace band' : p.onCopy ? 'erase on a copy (original kept)' : 'erase in place') : '', ...p.reasons, p.reviewed ? 'reviewed' : ''].filter(Boolean).join(' · ')}</span>
            </div>
          {/each}
        </div>
      </section>
    {:else}
      <section class="card wide">
        {#if step === 'running'}
          <h3 aria-live="polite">Saving… {Math.min(doneN, total)} of {total}</h3>
        {:else}
          <div class="result {outcome.tone}">
            <span class="ricon" aria-hidden="true"><Icon name={outcome.tone === 'ok' ? 'check' : 'warn'} size={18} stroke={2.2} /></span>
            <h3>{outcome.title}</h3>
          </div>
        {/if}
        <div class="bar" class:indet={step === 'running' && doneN === 0} role="progressbar" aria-label="Save progress" aria-valuemin={0} aria-valuemax={total} aria-valuenow={doneN}><div style="width:{(doneN / total) * 100}%"></div></div>
        {#if step === 'running'}
          <p class="lead">Each photo is written to a temporary file, checked, and only then put in place. You can keep working in the editor meanwhile.</p>
        {:else}
          <ul class="lead lines">{#each outcome.lines as l}<li>{l}</li>{/each}</ul>
        {/if}
        <div class="stats row">
          <span class="stat ok"><b>{running.done || 0}</b> saved</span>
          <span class="stat bad" data-tip="Couldn’t be saved; the list says why."><b>{running.failed || 0}</b> failed</span>
          {#if skippedN}<span class="stat" data-tip="Skipped, excluded, or changed on disk during the batch"><b>{skippedN}</b> skipped</span>{/if}
          {#if running.held}<span class="stat warn" data-tip="Needed a look when the photos were checked, so they were held back"><b>{running.held}</b> held back</span>{/if}
          {#if running.blocked}<span class="stat bad" data-tip="Checking found they can’t be saved"><b>{running.blocked}</b> blocked</span>{/if}
          {#if running.notPrepared}<span class="stat bad" data-tip="Couldn’t be prepared again after the batch stopped"><b>{running.notPrepared}</b> not prepared</span>{/if}
          {#if running.cancelled}<span class="stat warn" data-tip="Not saved because the batch was stopped"><b>{running.cancelled}</b> stopped</span>{/if}
          {#if running.restored}<span class="stat" data-tip="Put back from the backups"><b>{running.restored}</b> restored</span>{/if}
          {#if step === 'running'}<span class="stat"><b>{Math.max(0, total - doneN)}</b> remaining</span>{/if}
          {#if step === 'running' && stagingBusy && staged < stageTotal && !cancelled}<span class="faint small">Preparing {staged} of {stageTotal}…</span>{/if}
        </div>
        {#if finished && summary?.canRestore && (running.done || 0) > 0}
          <div class="restore">
            <span class="ricon" aria-hidden="true"><Icon name="restore" size={18} /></span>
            <div class="grow">
              <b>Changed your mind?</b>
              <div class="small muted">“Restore originals” puts back the originals this batch overwrote, from the backups kept before saving ({backupsWhere}). The captioned versions are removed.</div>
            </div>
            <button class="btn danger" data-tip="Put the original photos back from their backups. Asks first." onclick={restore}><Icon name="restore" size={14} /> Restore originals…</button>
          </div>
        {/if}
        {#if step === 'running'}
          <div class="plist scroll">
            {#each (summary?.entries || []).filter((e: any) => e.state !== 'done') as e (e.index)}
              <div class="prow row">
                <span class="pill {entryPill[e.state] || 'skipped'}">{entryLabels[e.state] || e.state}</span>
                <span class="name" data-tip={e.path}>{rel(e.path || '')}</span>
                <span class="reasons" data-tip={e.error || e.out || undefined}>{e.error || (e.state === 'restored' ? 'original put back' : e.out ? rel(e.out) : '')}</span>
              </div>
            {/each}
          </div>
        {:else}
          <Disclosure id="batch.details" label="Each photo" count={`(${(summary?.entries || []).length})`} tip="What happened to every photo, and where it was saved." bind:open={detailsOpen}>
            <div class="plist scroll fixed">
              {#each summary?.entries || [] as e (e.index)}
                <div class="prow row">
                  <span class="pill {entryPill[e.state] || 'skipped'}">{entryLabels[e.state] || e.state}</span>
                  <span class="name" data-tip={e.path}>{rel(e.path || '')}</span>
                  <span class="reasons" data-tip={e.error || e.out || undefined}>{e.error || (e.state === 'restored' ? 'original put back' : e.out ? `saved as ${rel(e.out)}` : '')}</span>
                </div>
              {/each}
            </div>
          </Disclosure>
        {/if}
      </section>
    {/if}
  </div>

  <div class="actions row">
    {#if step === 'setup'}
      <span class="grow faint small summaryline">{#if files.length}{plural(pickedN, 'photo', 'photos')} · {app.template(templateId)?.name || ''} · {B.saveMode === 'overwrite' ? 'overwrite originals' : 'save copies'}{:else}Choose a folder to begin.{/if}</span>
      <button class="btn" class:primary={!!files.length} disabled={!files.length || (B.which !== 'all' && !selected.size)}
        data-tip={!files.length ? 'Choose a folder of photos first.' : B.which !== 'all' && !selected.size ? 'Tick at least one photo in the photo list.' : 'Look at every photo and its caption before anything is saved.'}
        onclick={preflight}>Check photos <Icon name="next" /></button>
    {:else if step === 'preflight'}
      <button class="btn ghost" data-tip="Go back to choosing photos and options. Nothing has been saved." onclick={() => { cancelled = true; stopPreflight(); step = 'setup' }}><Icon name="prev" size={14} /> Back</button>
      <span class="grow faint small">{pfReady && heldBack.length ? `${plural(heldBack.length, 'photo', 'photos')} held back unless reviewed.` : ''}</span>
      <button class="btn" disabled={!pfReady || !counts.flagged} data-tip={!pfReady ? 'Wait until every photo is checked.' : !counts.flagged ? 'No photos need a look.' : 'Open only the photos that need a look in the editor, one by one.'} onclick={() => review('flagged')}>{counts.flagged ? `Review ${counts.flagged} needing a look` : 'Review flagged'}</button>
      <button class="btn" disabled={!pfReady} data-tip={!pfReady ? 'Wait until every photo is checked.' : 'Step through every photo in the editor before saving. Optional.'} onclick={() => review('all')}>Review all</button>
      <button class="btn primary" disabled={!pfReady || !toSave.length} data-tip={!pfReady ? 'Wait until every photo is checked.' : !toSave.length ? 'No photos are ready to save. Review the ones that need a look, or go back and change the options.' : B.saveMode === 'overwrite' ? 'Replace the originals with captioned versions. Asks first.' : 'Save captioned copies. Asks first.'} onclick={saveAll}>{saveLabel(toSave.length)} <Icon name="next" /></button>
    {:else if step === 'running'}
      <span class="grow"></span>
      <button class="btn danger" data-tip="Stop after the photos being saved right now. Photos already saved stay saved." onclick={cancelRun}><Icon name="stop" size={14} /> Stop</button>
    {:else}
      <button class="btn ghost" data-tip="Save a spreadsheet (CSV) listing every photo and what happened to it." onclick={() => download(`/api/batch/${batchId}/report.csv`, `photoband-batch-${batchId}.csv`)}><Icon name="download" size={14} /> Export report</button>
      {#if (running.done || 0) > 0 && (summary?.meta?.settings?.saveMode || B.saveMode) !== 'overwrite'}<button class="btn ghost" data-tip="Show the saved copies in your file manager." onclick={showInFolder}><Icon name="folder" size={14} /> Show copies</button>{/if}
      <span class="grow"></span>
      {#if retryLabel}<button class="btn" data-tip="Try the photos that failed or were stopped again." onclick={retry}><Icon name="reset" size={14} /> {retryLabel}</button>{/if}
      <button class="btn" data-tip="Start another batch with the same folder." onclick={newBatch}>New batch</button>
      <button class="btn primary" data-tip="Go back to the editor." onclick={() => { app.view = 'editor' }}>Done</button>
    {/if}
  </div>
</div>

<style>
  .batch { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .steps { padding: 8px 16px; border-bottom: 1px solid var(--line); background: var(--bg-2); gap: 18px; min-height: 44px; }
  .stepper { display: flex; align-items: center; gap: 10px; list-style: none; margin: 0; padding: 0; flex-wrap: wrap; }
  .stepper li { display: flex; align-items: center; }
  .line { width: 28px; height: 2px; border-radius: 1px; background: var(--line-2); }
  .line.done { background: var(--ok); }
  .step { gap: 7px; color: var(--fg-3); font-size: 12.5px; white-space: nowrap; }
  .num { width: 22px; height: 22px; border-radius: 50%; display: inline-grid; place-items: center; font-size: 11px; font-weight: 600; border: 1.5px solid var(--line-2); color: var(--fg-3); flex: none; transition: background 0.15s, border-color 0.15s; }
  .step.past { color: var(--fg-2); }
  .step.past .num { background: color-mix(in srgb, var(--ok) 16%, transparent); border-color: var(--ok); color: var(--ok); }
  .step.on { color: var(--fg); font-weight: 700; }
  .step.on .num { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
  .body { flex: 1; min-height: 0; padding: 16px; }
  .stack { display: flex; flex-direction: column; gap: 16px; max-width: 760px; margin: 0 auto; }
  .card { background: var(--bg-2); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  .card.wide { max-width: 1000px; margin: 0 auto; height: 100%; }
  h3 { margin: 0; font-size: 14px; }
  .empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 10px; padding: 8px 0 4px; }
  .empty p { margin: 0; max-width: 46ch; color: var(--fg-2); line-height: 1.45; }
  .big { color: var(--fg-3); }
  .folder { gap: 10px; }
  .ficon { color: var(--fg-3); display: inline-grid; }
  .fname { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chk { gap: 6px; color: var(--fg-2); min-height: 28px; align-self: flex-start; }
  .set { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 4px 14px; align-items: start; }
  .set .k { color: var(--fg-2); padding-top: 5px; }
  .v { display: flex; flex-direction: column; gap: 5px; align-items: flex-start; min-width: 0; }
  .full { width: 100%; }
  .link { border: 0; background: none; padding: 0; color: var(--accent-link); cursor: pointer; font: inherit; text-decoration: underline; text-underline-offset: 2px; }
  .link:focus-visible { outline: none; box-shadow: var(--focus); border-radius: 3px; }
  .files { max-height: 240px; border: 1px solid var(--line); border-radius: 7px; padding: 4px; margin-top: 4px; }
  .frow { padding: 3px 6px; gap: 8px; border-radius: 5px; min-height: 28px; }
  .frow:hover { background: var(--bg-hover); }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .small { font-size: 12px; }
  .form { display: grid; grid-template-columns: 170px 1fr; gap: 12px 14px; align-items: center; margin-bottom: 10px; }
  .form > span { color: var(--fg-2); }
  .help { display: block; font-size: 11.5px; color: var(--fg-3); line-height: 1.3; margin-top: 1px; }
  .bar { height: 6px; background: var(--bg-3); border-radius: 4px; overflow: hidden; flex: none; }
  .bar div { height: 100%; background: var(--accent); transition: width 0.3s; }
  .bar.indet div { width: 30% !important; animation: indet 1.2s ease-in-out infinite; transition: none; }
  @keyframes indet { from { transform: translateX(-100%) } to { transform: translateX(340%) } }
  .note { color: var(--info-fg); background: var(--info-bg); padding: 3px 8px; border-radius: 5px; align-self: flex-start; }
  .note.warn { color: var(--warn-fg); background: var(--warn-bg); }
  .col { display: flex; flex-direction: column; gap: 5px; align-items: flex-start; min-width: 0; }
  .lead { margin: 0; color: var(--fg-2); line-height: 1.5; }
  .lines { padding-left: 18px; }
  .lines li + li { margin-top: 2px; }
  .stats { gap: 18px; flex-wrap: wrap; }
  .stat b { font-size: 18px; margin-right: 4px; }
  .stat.ok b { color: var(--ok); }
  .stat.warn b { color: var(--warn); }
  .stat.bad b { color: var(--danger); }
  .result { display: flex; align-items: center; gap: 10px; }
  .result h3 { font-size: 16px; }
  .ricon { width: 30px; height: 30px; border-radius: 50%; display: inline-grid; place-items: center; flex: none; }
  .result.ok .ricon { background: color-mix(in srgb, var(--ok) 16%, transparent); color: var(--ok); }
  .result.warn .ricon { background: var(--warn-bg); color: var(--warn); }
  .restore { display: flex; align-items: center; gap: 12px; border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; background: var(--bg); }
  .restore .ricon { background: var(--bg-3); color: var(--fg-2); }
  .plist { flex: 1; min-height: 120px; border: 1px solid var(--line); border-radius: 7px; }
  .plist.fixed { flex: none; max-height: 320px; }
  .prow { display: grid; grid-template-columns: 96px minmax(0, 1fr) minmax(0, 1.3fr); align-items: center; padding: 5px 10px; gap: 12px; font-size: 12.5px; min-height: 30px; }
  .prow + .prow { border-top: 1px solid var(--line); }
  .reasons { color: var(--fg-2); font-size: 12px; text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pill { font-size: 10.5px; font-weight: 600; padding: 1px 7px; border-radius: 9px; background: var(--bg-3); color: var(--fg-2); justify-self: start; min-width: 64px; text-align: center; white-space: nowrap; }
  .pill.restored { background: var(--info-bg); color: var(--info-fg); }
  .pill.ready { background: color-mix(in srgb, var(--ok) 18%, transparent); color: var(--ok); }
  .pill.flagged { background: var(--warn-bg); color: var(--warn-fg); }
  .pill.error, .pill.blocked { background: var(--err-bg); color: var(--err-fg); }
  .pill.pending { background: var(--info-bg); color: var(--info-fg); }
  .tag { font-size: 10.5px; padding: 0 6px; margin-right: 6px; border-radius: 8px; border: 1px solid var(--line-2); color: var(--fg-2); white-space: nowrap; }
  .actions { padding: 10px 16px; border-top: 1px solid var(--line); background: var(--bg-2); gap: 8px; min-height: 52px; }
  .summaryline { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  @media (prefers-reduced-motion: reduce) {
    .bar div, .num { transition: none; }
  }
</style>
