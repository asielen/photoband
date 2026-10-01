<script lang="ts">
  import { app } from '../lib/store.svelte'
  import { dialogs } from '../lib/dialogs.svelte'
  import { actions } from '../lib/actions'
  import Icon from './Icon.svelte'
  import Menu from './Menu.svelte'

  const s = $derived(app.session)
  const blocked = $derived(app.canSave(s))
  // a scan with a physical caption: Overwrite is off in every mode until Settings › Saving allows it
  const caseC = $derived(app.overwriteBlocked(s))
  // editing controls only make sense in the editor with photos open
  const editing = $derived(app.view !== 'batch' && app.photos.length > 0)
  const saveTip = $derived(app.saving ? 'Saving…' : blocked || 'Save a captioned copy, then go to the next photo. Your original file is not changed.')
  const backups = $derived(!!app.settings.saving.backupOriginals)
  const backupTip = $derived(backups
    ? 'Backup is on: before Overwrite replaces a photo, the photo is first copied to the backup folder (the strip below shows the exact file). Click to turn off.'
    : 'Backup is off: Overwrite replaces the photo and it can’t be recovered. Click to turn on (recommended).')
  function toggleBackups() {
    app.saveSettings({ saving: { backupOriginals: !backups } }).catch((e) => app.toast('error', e.message))
  }
  const overwriteTip = $derived(app.saving ? 'Saving…' : caseC || blocked || (app.settings.saving.backupOriginals ? 'Replace the file with the captioned version, then go to the next photo. A backup copy is kept first (the strip below shows where).' : 'Replace the file with the captioned version, then go to the next photo. Backups are off: it can’t be recovered.'))
</script>

<header class="tb">
  <div class="brand row" data-tip="Photoband {app.version}">
    <svg width="20" height="20" viewBox="0 0 64 64" aria-hidden="true"><rect x="8" y="4" width="48" height="56" rx="3" fill="#f4f1ea" stroke="currentColor" stroke-width="3"/><rect x="14" y="10" width="36" height="32" fill="#4a6fa5"/><path d="M20 50h24" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>
    <span>Photoband</span>
  </div>

  <Menu label="Open" tip="Open a folder or photos" items={[
    { label: 'Open folder…', hint: 'Mod+Shift+O', run: actions.openFolder },
    { label: 'Open files…', hint: 'Mod+O', run: actions.openFiles },
    { label: 'Close all', sep: true, disabled: !app.photos.length, why: 'No photos are open.', run: () => app.closeAllPhotos() },
  ]}>
    {#snippet trigger()}<Icon name="folder" /> Open <Icon name="chevdown" size={12} />{/snippet}
  </Menu>

  {#if editing}
  <span class="sep"></span>
  <label class="row tpl" class:review={!!app.batchReview}>
    <!-- in batch review the choice is a per-photo override inside the batch (the batch template is set in the batch setup) -->
    {#if app.batchReview}<span class="muted tlabel long">Template (this photo)</span><span class="muted tlabel short">This photo</span>{:else}<span class="muted tlabel">Template</span>{/if}
    <select class="field" disabled={!s} value={s?.draft.templateId ?? app.defaultTemplateId()} onchange={(e) => s && app.setTemplate(s, (e.target as HTMLSelectElement).value)} aria-label="Template" data-tip={!s ? 'Open a photo first.' : app.batchReview ? 'Template for this photo only. The batch template is chosen in the batch setup.' : 'The look of the band and which details go in it. Edited text is kept when you switch.'}>
      {#each app.templates as t}<option value={t.id}>{t.name}{t.builtin ? '' : ' (yours)'}</option>{/each}
    </select>
    <button class="btn ghost icon" data-tip="Edit templates and caption formats" aria-label="Edit templates" onclick={() => (dialogs.settingsOpen = 'formats')}><Icon name="pen" size={14} /></button>
  </label>

  {/if}

  <span class="grow"></span>

  {#if editing}
  <div class="row group undo">
    <button class="btn ghost icon" data-tip={!s || !s.undoStack.length ? 'Nothing to undo yet.' : 'Undo'} data-tip-key="Mod+Z" aria-label="Undo" disabled={!s || !s.undoStack.length} onclick={() => s && app.undo(s)}><Icon name="undo" /></button>
    <button class="btn ghost icon" data-tip={!s || !s.redoStack.length ? 'Nothing to redo.' : 'Redo'} data-tip-key="Mod+Shift+Z" aria-label="Redo" disabled={!s || !s.redoStack.length} onclick={() => s && app.redo(s)}><Icon name="redo" /></button>
  </div>
  <span class="sep undo"></span>
  {#if app.batchReview}
  <!-- in batch review nothing is saved one by one: the batch saves everything (Done reviewing → Save all) -->
  <span class="muted reviewnote" data-tip="In batch review, edits are saved with Save all.">Saved with the batch</span>
  {:else}
  <!-- Two ways to save, both moving on to the next photo: a copy (primary; the original is
       untouched) or overwrite (replaces the file, with a backup). Save copy as… and the
       marker-free copy are in the command palette. -->
  <div class="row group" role="group" aria-label="Save">
    <button class="btn primary squeeze save" disabled={!!blocked || app.saving} aria-busy={app.savingMode === 'copy'} aria-label="Save copy &amp; next" data-tip={saveTip} data-tip-key="Mod+S" onclick={actions.saveAndNext}>
      {#if app.savingMode === 'copy'}<span class="spin" aria-hidden="true"></span> Saving…{:else}<Icon name="save" /><span class="lbl long">Save copy &amp; next</span><span class="lbl short">Save copy</span><Icon name="next" />{/if}
    </button>
    <button class="btn squeeze overwrite" class:danger={!backups} class:solid={!backups} disabled={!!blocked || app.saving || !!caseC} aria-busy={app.savingMode === 'overwrite'} aria-label="Overwrite original &amp; next" data-tip={overwriteTip} data-tip-key="Mod+Shift+Enter" onclick={actions.overwriteAndNext}>
      {#if app.savingMode === 'overwrite'}<span class="spin dark" aria-hidden="true"></span> Saving…{:else}<Icon name={backups ? 'overwrite' : 'warn'} /><span class="lbl long">Overwrite &amp; next</span><span class="lbl short">Overwrite</span><Icon name="next" />{/if}
    </button>
    <!-- backups only matter for Overwrite: shown beside it so the pair reads as one decision -->
    <button class="btn ghost backup" class:off={!backups} role="switch" aria-checked={backups} aria-label="Back up photos before overwriting" disabled={app.saving} data-tip={app.saving ? 'Wait until the save finishes: it already uses the current setting.' : backupTip} onclick={toggleBackups}><Icon name={backups ? 'shieldcheck' : 'shield'} /><span class="lbl">Backup {backups ? 'on' : 'off'}</span></button>
  </div>
  {/if}
  <span class="sep"></span>
  {/if}
  <button class="btn ghost batchbtn" aria-label="Batch" class:on={app.view === 'batch'} aria-pressed={app.view === 'batch'} onclick={() => (app.view = app.view === 'batch' ? 'editor' : 'batch')} data-tip={app.view === 'batch' ? 'Back to the single-photo editor' : 'Caption a whole folder with one template'}><Icon name="batch" /><span class="lbl">Batch</span></button>
  <button class="btn ghost icon" data-tip="Find any command" data-tip-key="Mod+K" aria-label="Commands" aria-haspopup="dialog" onclick={() => (app.paletteOpen = true)}><Icon name="search" /></button>
  <button class="btn ghost icon" data-tip="Settings" data-tip-key="Mod+," aria-label="Settings" onclick={() => (dialogs.settingsOpen = 'general')}><Icon name="settings" /></button>
  <Menu label="Help" align="right" tip="Help, shortcuts and the save log" items={[
    { label: 'Getting started', tip: 'A short guide to captioning your first photo', run: () => (dialogs.helpOpen = 'start') },
    { label: 'Keyboard shortcuts', hint: 'F1', run: () => (dialogs.helpOpen = 'shortcuts') },
    { label: 'Save log', tip: 'Every file saved in this session', run: () => (dialogs.helpOpen = 'log') },
    { label: 'About and licenses', run: () => (dialogs.helpOpen = 'about') },
  ]}>
    {#snippet trigger()}<Icon name="help" />{/snippet}
  </Menu>
</header>

<style>
  /* never overflows: at the 960 px minimum window everything, Settings and Help included, fits.
     Save copy and Overwrite always keep their text labels; narrower windows drop, in order,
     the brand name and the "Template" label, then undo/redo (they have shortcuts). */
  .tb { height: 48px; display: flex; align-items: center; gap: 4px; padding: 0 12px; background: var(--bg-2); border-bottom: 1px solid var(--line); flex: none; min-width: 0; overflow: hidden; }
  .tb > :global(*) { flex-shrink: 0; }
  .tb > .grow { flex-shrink: 1; min-width: 0; }
  .brand { font-weight: 650; gap: 8px; margin-right: 8px; color: var(--fg); letter-spacing: -0.01em; }
  .tpl { gap: 6px; flex-shrink: 1; min-width: 0; }
  .tpl select { width: 190px; min-width: 110px; flex-shrink: 1; }
  .tlabel { white-space: nowrap; }
  .reviewnote { font-size: 12px; white-space: nowrap; }
  .tlabel.short { display: none; }
  .group { gap: 4px; }
  .btn .lbl { display: inline; }
  .btn .lbl.short { display: none; }
  .btn.squeeze { gap: 6px; }
  .save { min-width: 104px; }
  .overwrite:not(.danger) :global(svg:first-child) { color: var(--warn); }
  .backup { gap: 5px; padding-left: 6px; padding-right: 8px; }
  .backup :global(svg) { color: var(--ok, #2e8b57); }
  .backup.off, .backup.off :global(svg) { color: var(--warn-fg); }
  .backup.off { background: var(--warn-bg); }
  .sep { margin: 10px 6px; }
  /* pressed toggle, not a second primary button */
  .btn.on { background: var(--bg-active); border-color: var(--line-2); box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.12); }
  .spin { width: 12px; height: 12px; border: 2px solid color-mix(in srgb, var(--accent-fg) 40%, transparent); border-top-color: var(--accent-fg); border-radius: 50%; animation: sp 0.8s linear infinite; }
  .spin.dark { border-color: color-mix(in srgb, var(--fg) 25%, transparent); border-top-color: var(--fg); }
  @keyframes sp { to { transform: rotate(360deg) } }
  @media (max-width: 1279px) {
    .brand span, .tpl:not(.review) .tlabel { display: none; }
    .tpl select { width: 160px; }
    .btn .lbl.long { display: none; }
    .btn .lbl.short { display: inline; }
  }
  @media (max-width: 1099px) {
    .undo { display: none; }
    .tpl.review .tlabel.long { display: none; }
    .tpl.review .tlabel.short { display: inline; }
  }
</style>
