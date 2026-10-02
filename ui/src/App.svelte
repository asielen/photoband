<script lang="ts">
  import { onMount } from 'svelte'
  import { post } from './lib/api'
  import { actions } from './lib/actions'
  import { dialogs } from './lib/dialogs.svelte'
  import { app } from './lib/store.svelte'
  import BatchView from './components/BatchView.svelte'
  import CoachStrip from './components/CoachStrip.svelte'
  import CommandPalette, { type Command } from './components/CommandPalette.svelte'
  import ConfirmDialog from './components/ConfirmDialog.svelte'
  import ExistingBanner from './components/ExistingBanner.svelte'
  import FileBrowser from './components/FileBrowser.svelte'
  import Filmstrip from './components/Filmstrip.svelte'
  import HelpDialog from './components/HelpDialog.svelte'
  import Icon from './components/Icon.svelte'
  import Inspector from './components/Inspector.svelte'
  import JpegNotice from './components/JpegNotice.svelte'
  import SavePreview from './components/SavePreview.svelte'
  import Preview from './components/Preview.svelte'
  import SettingsDialog from './components/SettingsDialog.svelte'
  import Toasts from './components/Toasts.svelte'
  import Toolbar from './components/Toolbar.svelte'
  import Warnings from './components/Warnings.svelte'

  let preview: Preview | undefined = $state()
  let tool = $state<'pan' | 'edge' | 'brush-add' | 'brush-remove'>('pan')
  let dragOver = $state(false)
  let batchMounted = $state(false)
  $effect(() => {
    if (app.view === 'batch') batchMounted = true
  })
  const mod = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl+'
  const TAB_NAMES = { text: 'Text', style: 'Style', layout: 'Layout', metadata: 'Metadata' } as const

  // every app action, for the command palette (Mod+K). Disabled ones say why, with the same
  // reasons the toolbar's tooltips give.
  const commands = $derived.by((): Command[] => {
    const s = app.session
    const inEditor = app.view === 'editor' && app.photos.length > 0
    const noPhoto = inEditor ? (s ? null : 'Open a photo first.') : app.view === 'batch' ? 'Go back to the editor first (Batch is open).' : 'Open a photo first.'
    const busy = app.saving ? 'Saving…' : null
    const blocked = app.canSave(s)
    const saveWhy = noPhoto || blocked || busy
    const overWhy = noPhoto || app.overwriteBlocked(s) || blocked || busy
    const out: Command[] = [
      { id: 'open-folder', group: 'Open', label: 'Open folder…', key: 'Mod+Shift+O', run: actions.openFolder },
      { id: 'open-files', group: 'Open', label: 'Open files…', key: 'Mod+O', run: actions.openFiles },
      { id: 'save-next', group: 'Save', label: 'Save copy & next', key: 'Mod+S', disabled: saveWhy, keywords: 'export write continue', run: actions.saveAndNext },
      { id: 'overwrite-next', group: 'Save', label: 'Overwrite original & next', key: 'Mod+Shift+Enter', disabled: overWhy, keywords: 'replace continue', run: actions.overwriteAndNext },
      { id: 'save-copy-as', group: 'Save', label: 'Save copy as…', key: 'Mod+Shift+S', disabled: saveWhy, run: actions.saveCopyAs },
      { id: 'save-no-marker', group: 'Save', label: 'Save copy without hidden band data', disabled: saveWhy, keywords: 'marker privacy', run: actions.removeMarker },
      { id: 'edit-caption', group: 'Caption', label: 'Edit the caption', key: 'E', disabled: noPhoto, keywords: 'text type', run: actions.focusFirstBlock },
      { id: 'undo', group: 'Caption', label: 'Undo', key: 'Mod+Z', disabled: noPhoto || (s?.undoStack.length ? null : 'Nothing to undo yet.'), run: () => s && app.undo(s) },
      { id: 'redo', group: 'Caption', label: 'Redo', key: 'Mod+Shift+Z', disabled: noPhoto || (s?.redoStack.length ? null : 'Nothing to redo.'), run: () => s && app.redo(s) },
      { id: 'revert', group: 'Caption', label: 'Revert to template', disabled: noPhoto || (s && app.hasEdits(s) ? null : 'Nothing to revert: this photo follows its template.'), run: actions.revertToTemplate },
    ]
    for (const t of app.templates) {
      const cur = s?.draft.templateId === t.id
      out.push({ id: `tpl-${t.id}`, group: 'Template', label: `Template: ${t.name}`, note: cur ? 'current' : t.builtin ? '' : 'yours', disabled: noPhoto, keywords: 'switch look style', run: () => s && app.setTemplate(s, t.id) })
    }
    out.push({ id: 'edit-templates', group: 'Template', label: 'Edit templates and caption formats…', keywords: 'settings formats', run: () => (dialogs.settingsOpen = 'formats') })
    for (const [id, name] of Object.entries(TAB_NAMES)) {
      out.push({ id: `tab-${id}`, group: 'Caption panel', label: `${name} tab`, disabled: noPhoto, note: app.inspectorTab === id && !app.panelsHidden ? 'showing' : '', keywords: 'inspector panel', run: () => app.reveal(id as keyof typeof TAB_NAMES) })
    }
    out.push(
      { id: 'faces', group: 'View', label: app.showFaces ? 'Hide faces' : 'Show faces', key: 'F', disabled: noPhoto, keywords: 'people names', run: () => (app.showFaces = !app.showFaces) },
      { id: 'zoom-fit', group: 'View', label: 'Zoom to fit', key: 'Mod+0', disabled: noPhoto, run: () => preview?.zoomFit() },
      { id: 'zoom-100', group: 'View', label: 'Zoom 1:1 (actual pixels)', key: 'Mod+1', disabled: noPhoto, keywords: '100%', run: () => preview?.zoom100() },
      { id: 'side', group: 'View', label: 'Before and after side by side', key: 'Y', disabled: noPhoto, keywords: 'layout panes', run: () => preview?.arrangeTo('side') },
      { id: 'stacked', group: 'View', label: 'Before above after (stacked)', key: 'Y', disabled: noPhoto, keywords: 'layout panes', run: () => preview?.arrangeTo('stacked') },
      { id: 'panels', group: 'View', label: app.panelsHidden ? 'Show side panels' : 'Hide side panels', key: 'Mod+\\', disabled: noPhoto, run: () => (app.panelsHidden = !app.panelsHidden) },
      { id: 'next', group: 'Photos', label: 'Next photo', key: '↓', disabled: noPhoto || (app.current >= app.photos.length - 1 ? 'This is the last photo.' : null), run: () => app.next() },
      { id: 'prev', group: 'Photos', label: 'Previous photo', key: '↑', disabled: noPhoto || (app.current <= 0 ? 'This is the first photo.' : null), run: () => app.prev() },
      { id: 'close-all', group: 'Photos', label: 'Close all photos', disabled: app.photos.length ? null : 'No photos are open.', run: () => app.closeAllPhotos() },
      { id: 'batch', group: 'App', label: app.view === 'batch' ? 'Back to the editor' : 'Batch: caption a whole folder', keywords: 'many folder', run: () => (app.view = app.view === 'batch' ? 'editor' : 'batch') },
      { id: 'settings', group: 'App', label: 'Settings', key: 'Mod+,', keywords: 'preferences options', run: () => (dialogs.settingsOpen = 'general') },
      { id: 'getting-started', group: 'App', label: 'Getting started', keywords: 'help guide tutorial how', run: () => (dialogs.helpOpen = 'start') },
      { id: 'shortcuts', group: 'App', label: 'Keyboard shortcuts', key: 'F1', keywords: 'help keys', run: () => (dialogs.helpOpen = 'shortcuts') },
      { id: 'save-log', group: 'App', label: 'Save log', keywords: 'help history', run: () => (dialogs.helpOpen = 'log') },
      { id: 'about', group: 'App', label: 'About and licenses', keywords: 'help version', run: () => (dialogs.helpOpen = 'about') },
    )
    return out
  })
  const lastFolderName = $derived((app.settings?.session?.lastFolder || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || '')

  onMount(() => {
    app.init()
    // drops from the native window (pywebview gives full paths)
    ;(window as any).__photobandDrop = (paths: string[]) => {
      app.view = 'editor'
      app.openPaths(paths)
    }
    const onBefore = () => {
      for (const s of app.sessions.values()) app.flushDraft(s)
    }
    window.addEventListener('beforeunload', onBefore)
    return () => window.removeEventListener('beforeunload', onBefore)
  })

  function typing(e: KeyboardEvent) {
    const t = e.target as HTMLElement
    return t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT'
  }
  // controls that use Space / arrow keys themselves
  const CONTROL = 'button, a[href], summary, [role="radio"], [role="tab"], [role="option"], [role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="listbox"], [role="slider"], [role="checkbox"], [role="switch"], [role="spinbutton"], [role="button"]'
  function onControl(e: KeyboardEvent) {
    const t = e.target as HTMLElement
    return !!t.closest?.(CONTROL)
  }
  /** Nothing in particular has focus: the page itself, the filmstrip or the preview. */
  function onCanvas(e: KeyboardEvent) {
    const t = e.target as HTMLElement
    return t === document.body || t === document.documentElement || !!t.closest?.('.preview') || !!t.closest?.('[role="listbox"][aria-label="Photos"]')
  }
  const modalOpen = () => !!(dialogs.confirmState || dialogs.browserState || dialogs.settingsOpen || dialogs.helpOpen || app.paletteOpen)

  function key(e: KeyboardEvent) {
    const m = e.metaKey || e.ctrlKey
    const k = e.key.toLowerCase()
    if (modalOpen()) return
    const s = app.session
    // Mod+K: every command, searchable (works while typing too)
    if (m && k === 'k' && !e.shiftKey && !e.altKey) {
      e.preventDefault()
      app.paletteOpen = true
      return
    }
    // saving moves on to the next photo: a held key (repeat) must not save photo after photo,
    // and the hidden editor photo is never saved from the Batch view
    if (m && (k === 's' || k === 'enter')) {
      e.preventDefault()
      if (e.repeat || app.view !== 'editor') return
      if (k === 's') {
        if (e.shiftKey) actions.saveCopyAs()
        else actions.saveAndNext()
      } else if (e.shiftKey) actions.overwriteAndNext()
      else actions.saveAndNext()
      return
    }
    if (m && k === 'o') {
      e.preventDefault()
      if (e.shiftKey) actions.openFolder()
      else actions.openFiles()
      return
    }
    if (m && e.key === ',') {
      e.preventDefault()
      dialogs.settingsOpen = 'general'
      return
    }
    if (e.key === 'F1') {
      e.preventDefault()
      dialogs.helpOpen = 'shortcuts'
      return
    }
    if (typing(e)) return
    if (m && k === 'z' && s) {
      e.preventDefault()
      if (e.shiftKey) app.redo(s)
      else app.undo(s)
      return
    }
    if (m && k === 'y' && s) {
      e.preventDefault()
      app.redo(s)
      return
    }
    if (m && e.key === '0') {
      e.preventDefault()
      preview?.zoomFit()
      return
    }
    if (m && e.key === '1') {
      e.preventDefault()
      preview?.zoom100()
      return
    }
    if (app.view !== 'editor' && !app.batchReview) return
    // Ctrl/Cmd+\ hides or shows the side panels (also a button in the preview's status bar).
    // Tab is never taken over: it always moves focus through the window.
    if (m && !e.altKey && (e.key === '\\' || e.code === 'Backslash') && app.photos.length) {
      e.preventDefault()
      app.panelsHidden = !app.panelsHidden
      return
    }
    const arrow = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown'].includes(e.key)
    const bare = e.target === document.body || e.target === document.documentElement
    const inStrip = !!(e.target as HTMLElement).closest?.('[role="listbox"][aria-label="Photos"]')
    // E (or Enter from the filmstrip / with nothing focused) jumps into the first caption block
    if (!m && !e.altKey && ((k === 'e' && !e.shiftKey && onCanvas(e)) || (e.key === 'Enter' && (bare || inStrip)))) {
      e.preventDefault()
      actions.focusFirstBlock()
      return
    }
    // buttons, tabs, radios, menus, sliders… keep their own Space and arrow behaviour
    if ((arrow || e.key === ' ' || e.key === 'Enter') && onControl(e)) return
    if (arrow && !onCanvas(e)) return
    if (tool === 'edge' && ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
      e.preventDefault()
      const d = e.shiftKey ? 10 : 1
      preview?.nudgeEdge(e.key === 'ArrowLeft' ? -d : e.key === 'ArrowRight' ? d : 0, e.key === 'ArrowUp' ? -d : e.key === 'ArrowDown' ? d : 0)
      return
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight' || e.key === 'PageDown') {
      e.preventDefault()
      app.next()
    } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft' || e.key === 'PageUp') {
      e.preventDefault()
      app.prev()
    } else if (k === 'f' && !m) {
      app.showFaces = !app.showFaces
    } else if (e.key === ' ' && onCanvas(e)) {
      e.preventDefault()
      app.showOriginal = true
    } else if (e.key === '?') {
      dialogs.helpOpen = 'shortcuts'
    }
  }
  function keyup(e: KeyboardEvent) {
    if (e.key === ' ') app.showOriginal = false
  }

  function drop(e: DragEvent) {
    e.preventDefault()
    dragOver = false
    // In the desktop window, pywebview reports full paths separately (window.__photobandDrop).
    if (app.mode === 'browser') app.toast('info', 'In browser mode, use Open to choose files. Dropping works in the desktop app.')
  }

  async function resume(b: any) {
    app.view = 'batch'
    ;(window as any).__photobandResume = b.id
  }
</script>

<svelte:window onkeydown={key} onkeyup={keyup} onblur={() => (app.showOriginal = false)} />

{#if app.fatal}
  <div class="fatal">
    <h1>Photoband couldn't start</h1>
    <p>{app.fatal}</p>
    <button class="btn primary" onclick={() => location.reload()}>Try again</button>
  </div>
{:else if !app.ready}
  <div class="loading"><span class="spin"></span></div>
{:else}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="shell" ondragover={(e) => { e.preventDefault(); dragOver = true }} ondragleave={(e) => { if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) dragOver = false }} ondrop={drop}>
    <Toolbar />
    {#if app.view === 'editor' && app.session && !app.batchReview}<SavePreview s={app.session} />{/if}
    {#if app.notice}<div class="notice row"><Icon name="info" size={14} /> {app.notice}</div>{/if}
    {#if app.incompleteBatches.length && app.view === 'editor'}
      {#each app.incompleteBatches as b}
        <div class="notice warn row">
          <Icon name="warn" size={14} />
          <span class="grow">A batch from {new Date(b.created * 1000).toLocaleString()} stopped with {b.pending} of {b.total} photos left.</span>
          <button class="btn sm primary" data-tip="Continue saving the photos that are left" onclick={() => resume(b)}>Resume batch</button>
          <button class="btn sm ghost" data-tip="Forget this batch. Files already saved stay saved." onclick={() => { post(`/api/batch/${b.id}/discard`); app.incompleteBatches = app.incompleteBatches.filter((x) => x.id !== b.id) }}>Discard</button>
        </div>
      {/each}
    {/if}
    {#if batchMounted}
      <div class="batchwrap" class:hidden={app.view !== 'batch'}><BatchView /></div>
    {/if}
    {#if app.view === 'batch'}
      <!-- batch view shown above -->
    {:else if !app.photos.length}
      <div class="welcome">
        <div class="card">
          <svg width="64" height="64" viewBox="0 0 64 64" aria-hidden="true"><rect x="8" y="4" width="48" height="56" rx="3" fill="#f4f1ea" stroke="currentColor" stroke-width="2.5"/><rect x="14" y="10" width="36" height="32" fill="#4a6fa5"/><path d="M14 36l10-10 8 8 6-5 12 11v2H14z" fill="#2e4b73"/><circle cx="41" cy="18" r="4" fill="#f6d365"/><path d="M20 50h24" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/></svg>
          <h1>Caption your photos</h1>
          <p class="lead">Photoband adds a Polaroid-style caption band to your scanned photos.</p>
          <p class="muted">Open a folder or a few photos{app.mode === 'desktop' ? ', or drag them into this window' : ''}. Captions fill in from each photo's title, date and the names of the people in it, and the preview shows exactly what will be saved.</p>
          <div class="row btns">
            <button class="btn primary big" data-tip="Open every photo in a folder" data-tip-key="Mod+Shift+O" onclick={actions.openFolder}><Icon name="folder" /> Open folder…</button>
            <button class="btn big" data-tip="Open one or more photos" data-tip-key="Mod+O" onclick={actions.openFiles}><Icon name="image" /> Open files…</button>
          </div>
          <div class="row btns2">
            <button class="btn ghost" data-tip="Caption a whole folder with one template, then review and save them all" onclick={() => (app.view = 'batch')}><Icon name="batch" /> Batch a folder…</button>
          </div>
          {#if app.settings.session.lastFolder}
            <p class="reopen"><button class="btn ghost" data-tip="Open {app.settings.session.lastFolder} again" onclick={actions.reopenLastFolder}><Icon name="folder" size={14} /> Reopen <span class="lf">{lastFolderName}</span></button></p>
          {/if}
          <p class="faint small">TIFF (8 and 16-bit), JPEG and PNG. Save copy leaves your original untouched.<br /><kbd>{mod}O</kbd> open files · <kbd>{mod}Shift+O</kbd> open folder · <kbd>{mod}K</kbd> every command</p>
        </div>
      </div>
    {:else}
      <div class="main" class:nopanels={app.panelsHidden}>
        {#if !app.panelsHidden}<Filmstrip />{/if}
        <section class="center">
          {#if app.batchReview}
            <div class="review row" data-tip="Edits you make here are used for this photo when the batch saves.">
              <Icon name="batch" size={14} />
              <b class="rv">Reviewing {app.current + 1} of {app.photos.length}</b>
              <span class="muted rvhint">· edits apply to this photo in the batch</span>
              <span class="grow"></span>
              <button class="btn sm" aria-label="Previous photo" data-tip={app.current <= 0 ? 'This is the first photo.' : 'Previous photo'} data-tip-key="↑" disabled={app.current <= 0} onclick={() => app.prev()}><Icon name="prev" size={14} /><span class="navlbl">Previous</span></button>
              <button class="btn sm" aria-label="Next photo" data-tip={app.current >= app.photos.length - 1 ? 'This is the last photo.' : 'Next photo'} data-tip-key="↓" disabled={app.current >= app.photos.length - 1} onclick={() => app.next()}><span class="navlbl">Next</span><Icon name="next" size={14} /></button>
              {#if app.photos[app.current]?.name.endsWith('(excluded)')}
                <button class="btn sm" data-tip="Put this photo back into the batch" onclick={() => { const p = app.session?.path; if (p) (window as any).__batchExclude?.(p) }}>Include again</button>
              {:else}
                <button class="btn sm danger" data-tip="Leave this photo out of the batch. The file is not changed." onclick={() => { const p = app.session?.path; if (p) (window as any).__batchExclude?.(p) }}>Exclude</button>
              {/if}
              <button class="btn sm primary" data-tip="Back to the batch, where Save all saves every photo" onclick={() => app.batchReview?.onExit()}>Done reviewing</button>
            </div>
          {/if}
          {#if app.session && !app.batchReview}<CoachStrip />{/if}
          {#if app.session}
            {#key app.session.path}
              <ExistingBanner s={app.session} bind:tool />
            {/key}
            {#if app.session.error}
              <div class="err row"><Icon name="warn" /> {app.session.error}</div>
            {/if}
            {#if !app.batchReview}<JpegNotice s={app.session} />{/if}
          {/if}
          <Preview bind:this={preview} bind:tool />
          {#if app.session}<Warnings s={app.session} />{/if}
        </section>
        {#if app.session && !app.panelsHidden}
          <Inspector s={app.session} />
        {/if}
      </div>
    {/if}
    {#if dragOver && app.mode === 'desktop'}<div class="drop"><div>Drop photos or a folder to open them</div></div>{/if}
  </div>
{/if}

{#if app.paletteOpen}<CommandPalette {commands} onclose={() => (app.paletteOpen = false)} />{/if}
<ConfirmDialog />
{#if dialogs.browserState}<FileBrowser />{/if}
{#if dialogs.settingsOpen}<SettingsDialog />{/if}
{#if dialogs.helpOpen}<HelpDialog />{/if}
<Toasts />

<style>
  .shell { height: 100%; display: flex; flex-direction: column; position: relative; }
  .batchwrap { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .batchwrap.hidden { display: none; }
  .review { padding: 7px 12px; background: var(--info-bg); color: var(--info-fg); border-bottom: 1px solid var(--line); gap: 8px; font-size: 12.5px; }
  .review .muted { color: inherit; opacity: 0.8; }
  /* one line at 960 px and up; wraps (never clips) if it ever has to */
  .review { flex-wrap: wrap; row-gap: 6px; white-space: nowrap; }
  .review .rv { flex: none; }
  .review .rvhint { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .review :global(.btn) { flex: none; }
  @media (max-width: 1279px) { .review .rvhint { display: none; } }
  @media (max-width: 1099px) { .review .navlbl { display: none; } }
  .main { flex: 1; min-height: 0; display: grid; grid-template-columns: 176px minmax(0, 1fr) auto; }
  .main.nopanels { grid-template-columns: minmax(0, 1fr); }
  @media (max-width: 1279px) { .main:not(.nopanels) { grid-template-columns: 136px minmax(0, 1fr) auto; } }
  .reopen { margin: -6px 0 14px; }
  .lf { max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
  .center { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
  .welcome { flex: 1; display: grid; place-items: center; padding: 24px; background: radial-gradient(ellipse at 50% 35%, var(--bg-2), var(--bg)); }
  .card { max-width: 560px; text-align: center; }
  .card h1 { font-size: 22px; margin: 14px 0 8px; letter-spacing: -0.015em; }
  .card p { line-height: 1.55; }
  .card p.lead { font-size: 14.5px; margin: 0 0 6px; color: var(--fg); }
  .btns { justify-content: center; margin: 24px 0 8px; flex-wrap: wrap; gap: 8px; }
  .btns2 { justify-content: center; margin: 0 0 16px; }
  .btn.big { height: 36px; padding: 0 16px; font-size: 13.5px; }
  .small { font-size: 12px; }
  .notice { padding: 6px 12px; font-size: 12.5px; background: var(--info-bg); color: var(--info-fg); border-bottom: 1px solid var(--line); }
  .notice.warn { background: var(--warn-bg); color: var(--warn-fg); }
  .err { padding: 8px 12px; background: var(--err-bg); color: var(--err-fg); }
  .drop { position: absolute; inset: 0; background: color-mix(in srgb, var(--accent) 18%, transparent); border: 3px dashed var(--accent); display: grid; place-items: center; pointer-events: none; z-index: 30; }
  .drop div { background: var(--bg-2); padding: 14px 20px; border-radius: 10px; font-weight: 600; box-shadow: var(--shadow); }
  .fatal { padding: 48px; max-width: 640px; margin: auto; }
  .loading { height: 100%; display: grid; place-items: center; }
  .spin { width: 22px; height: 22px; border: 3px solid var(--line-2); border-top-color: var(--accent); border-radius: 50%; animation: sp 0.8s linear infinite; }
  @keyframes sp { to { transform: rotate(360deg) } }
</style>
