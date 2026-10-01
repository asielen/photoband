// User actions shared by the toolbar, menus and keyboard shortcuts.
import { post } from './api'
import { dialogs } from './dialogs.svelte'
import { app, type PhotoSession } from './store.svelte'

const JPEG_NOTE = 'Saving a JPEG always re-compresses the photo, which loses a little quality each time. TIFF and PNG are saved losslessly.'

function outputIsJpeg(s: PhotoSession, destPath?: string): boolean {
  if (destPath) return /\.jpe?g$/i.test(destPath)
  const fmt = app.settings.saving.outputFormat
  return fmt === 'jpeg' || (fmt === 'same' && s.meta?.info.format === 'JPEG')
}

/** Shown once: however it is closed (button, × or Esc), it counts as seen. */
async function jpegWarning(destPath?: string): Promise<boolean> {
  const s = app.session
  if (!s?.meta) return true
  if (!outputIsJpeg(s, destPath) || app.settings.session.jpegWarned) return true
  const r = await dialogs.ask('JPEG is re-encoded', `${JPEG_NOTE}\n\nThis message appears once.`, [
    { id: 'cancel', label: 'Cancel' },
    { id: 'ok', label: 'Continue', kind: 'primary' },
  ])
  app.saveSettings({ session: { jpegWarned: true } }).catch(() => {})
  return r.id === 'ok'
}

/** Saving waits for the existing-caption check: it decides where the photo is in the file. */
async function analysisReady(s: PhotoSession): Promise<boolean> {
  if (s.existingLoading) {
    app.toast('info', 'Checking for an existing caption first…', undefined, 3000)
    await app.existingReady(s)
  }
  // the user may have moved on to another photo meanwhile
  return app.session === s && !s.retired
}

/** Case B/C photos where the user hasn't chosen what to do with the existing band. */
async function existingBandCheck(s: PhotoSession): Promise<boolean> {
  if (!(await analysisReady(s))) return false
  const why = app.canSave(s)
  if (why) {
    app.toast('warn', why)
    return false
  }
  const ex = s.existing
  if (!ex || (ex.case !== 'B' && ex.case !== 'C')) return true
  if (s.draft.mode !== 'band' || s.draft.existingChoice || s.existingIgnored) return true
  const r = await dialogs.ask('This photo already has a caption band', 'Saving adds a new band below it. Choose how to handle the existing caption first, or keep it as part of the photo.', [
    { id: 'cancel', label: 'Cancel' },
    { id: 'choose', label: 'Choose…' },
    { id: 'anyway', label: 'Save anyway', kind: 'primary' },
  ])
  if (r.id === 'anyway') {
    s.existingIgnored = true
    return true
  }
  if (r.id === 'choose') {
    // the choices are in the banner above the preview
    setTimeout(() => document.querySelector<HTMLElement>('.banner button')?.focus(), 0)
  }
  return false
}

function splitPath(p: string) {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  return { dir: i >= 0 ? p.slice(0, i) || p.slice(0, 1) : '', name: i >= 0 ? p.slice(i + 1) : p }
}

/** Same file? Windows and macOS file systems ignore letter case (the backend double-checks). */
function samePath(a: string, b: string): boolean {
  const norm = (p: string) => {
    let n = p.replace(/\\/g, '/').replace(/\/+$/, '')
    if (app.platform === 'win32' || app.platform === 'darwin') n = n.toLowerCase()
    return n
  }
  return norm(a) === norm(b)
}

async function saveCopy(): Promise<boolean> {
  const s = app.session
  if (!s || !(await existingBandCheck(s)) || !(await jpegWarning())) return false
  let res = await app.save(s, 'copy')
  if (res && !res.ok && res.code === 'exists') {
    const { dir, name } = splitPath(res.error)
    const r = await dialogs.ask('File already exists', `“${name}” already exists in ${dir}. Replace it or keep both?`, [
      { id: 'cancel', label: 'Cancel' },
      { id: 'increment', label: 'Keep both' },
      { id: 'overwrite', label: 'Replace', kind: 'danger' },
    ])
    if (r.id !== 'increment' && r.id !== 'overwrite') return false
    res = await app.save(s, 'copy', { onExists: r.id })
  }
  return !!res?.ok
}

async function overwrite(): Promise<boolean> {
  const s = app.session
  if (!s?.meta) return false
  // the case C guard below needs the finished check
  if (!(await analysisReady(s))) return false
  // a physical caption is protected in every mode (also "Ignore": a new band below the handwriting)
  const off = app.overwriteBlocked(s)
  if (off) {
    app.toast('warn', off)
    return false
  }
  if (!(await existingBandCheck(s))) return false
  const jpeg = outputIsJpeg(s, s.path) && !app.settings.session.jpegWarned
  if (!app.overwriteConfirmed) {
    const backup = app.settings.saving.backupOriginals
      ? `The untouched file is copied to ${app.settings.saving.backupFolder || 'an “_originals” folder next to it'} first.`
      : 'Backups are turned OFF: the original cannot be recovered.'
    // one dialog: the JPEG note joins the confirmation instead of following it
    const r = await dialogs.ask('Overwrite the original?', `${s.meta.name} will be replaced by the captioned version. ${backup}${jpeg ? `\n\n${JPEG_NOTE}` : ''}`, [
      { id: 'cancel', label: 'Cancel' },
      { id: 'ok', label: 'Overwrite', kind: 'danger' },
    ], "Don't ask again this session")
    if (jpeg) app.saveSettings({ session: { jpegWarned: true } }).catch(() => {})
    if (r.id !== 'ok') return false
    if (r.checked) app.overwriteConfirmed = true
  } else if (!(await jpegWarning(s.path))) {
    return false
  }
  const res = await app.save(s, 'overwrite')
  return !!res?.ok
}

/** After a save: open the next photo, keeping the caption block that was being edited. */
async function goToNext(blk: string | null) {
  if (app.current >= app.photos.length - 1) {
    app.toast('info', 'That was the last photo.')
    return
  }
  await app.next()
  const s = app.session
  const eff = s ? app.effective(s) : null
  if (blk && eff) {
    app.inspectorTab = 'text'
    app.focusBlock = eff.blocks.some((b) => b.id === blk) ? blk : eff.blocks[0]?.id ?? null
  }
}

/** The caption block that has focus (or had it just before a toolbar click). */
function focusedBlock(): string | null {
  const a = document.activeElement as HTMLElement | null
  const own = a?.closest?.('[data-block]')?.getAttribute('data-block')
  if (own) return own
  if (!a || a === document.body || a.closest?.('header')) return app.lastBlock
  return null
}

export const actions = {
  async openFolder() {
    const p = await dialogs.pick('open-folder', 'Open a folder of photos', app.settings.session.lastFolder)
    if (p?.length) {
      app.view = 'editor'
      await app.openPaths(p)
    }
  },
  async openFiles() {
    const p = await dialogs.pick('open-files', 'Open photos', app.settings.session.lastFolder)
    if (p?.length) {
      app.view = 'editor'
      await app.openPaths(p)
    }
  },
  saveCopy: () => saveCopy(),
  async saveCopyAs() {
    const s = app.session
    if (!s?.meta) return
    const stem = s.meta.name.replace(/\.[^.]+$/, '')
    const ext = s.meta.name.split('.').pop()
    const dir = splitPath(s.path).dir
    const p = await dialogs.pick('save-file', 'Save copy as', dir, `${stem}-captioned.${ext}`)
    if (!p?.length) return
    const dest = p[0]
    // saving "a copy" onto the source is an overwrite: same confirmation, backup and guards
    if (samePath(dest, s.path)) {
      await overwrite()
      return
    }
    if (!(await existingBandCheck(s)) || !(await jpegWarning(dest))) return
    let res = await app.save(s, 'copyAs', { destPath: dest })
    if (res && !res.ok && res.code === 'source') {
      await overwrite()
      return
    }
    if (res && !res.ok && res.code === 'exists') {
      const { dir: d, name } = splitPath(res.error || dest)
      const r = await dialogs.ask(`Replace “${name}”?`, `“${name}” already exists in ${d}. Replacing it can't be undone.`, [
        { id: 'cancel', label: 'Cancel' },
        { id: 'overwrite', label: 'Replace', kind: 'danger' },
      ])
      if (r.id !== 'overwrite') return
      res = await app.save(s, 'copyAs', { destPath: dest, onExists: 'overwrite' })
    }
  },
  overwrite: () => overwrite(),
  /** Save a copy, then go to the next photo (Mod+Enter). */
  async saveAndNext() {
    const blk = focusedBlock()
    if (await saveCopy()) await goToNext(blk)
  },
  /** Overwrite the original, then go to the next photo (Mod+Shift+Enter). */
  async overwriteAndNext() {
    const blk = focusedBlock()
    if (await overwrite()) await goToNext(blk)
  },
  /** E / Enter: jump into the first caption block. */
  focusFirstBlock() {
    const s = app.session
    const eff = s ? app.effective(s) : null
    if (!eff?.blocks.length) return
    app.panelsHidden = false
    app.inspectorTab = 'text'
    app.focusBlock = eff.blocks[0].id
  },
  async revertToTemplate() {
    const s = app.session
    if (!s || !app.hasEdits(s)) return
    const ok = await dialogs.confirm('Revert to template?', "This photo's text and style edits are discarded and every block follows the template again. You can undo this.", 'Revert', true)
    if (ok) app.revertToTemplate(s)
  },
  async reopenLastFolder() {
    const folder = app.settings.session.lastFolder
    if (!folder) return
    try {
      // the folder must be allowed again in this session before it can be opened
      await post('/api/fs/allow', { paths: [folder] })
      app.view = 'editor'
      await app.openPaths([folder])
    } catch (e: any) {
      app.toast('error', `Couldn't reopen ${folder}: ${e.message}`)
    }
  },
  async removeMarker() {
    const s = app.session
    if (!s) return
    if (!(await existingBandCheck(s)) || !(await jpegWarning())) return
    const res = await app.save(s, 'copy', { embedMarker: false })
    if (res?.ok) app.toast('info', 'Saved a copy without the hidden band data.')
  },
}
