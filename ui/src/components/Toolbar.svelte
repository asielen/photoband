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
  const saveTip = $derived(app.saving ? 'Saving…' : blocked || 'Save a captioned copy. Your original file is not changed.')
  const overwriteTip = $derived(app.saving ? 'Saving…' : caseC || blocked || (app.settings.saving.backupOriginals ? 'Replace the original file with the captioned version. A backup copy of the original is kept.' : 'Replace the original file with the captioned version. Backups are turned off in Settings.'))
  const nextTip = $derived(app.saving ? 'Saving…' : blocked || 'Save a copy, then go to the next photo')
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
      {#each app.templates as t}<option value={t.id}>{t.name}{t.builtin ? '' : ' ★'}</option>{/each}
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
  <!-- Two ways to save, each spelled out: a copy (primary; the original is untouched) and
       overwrite (replaces the file, with a backup). Each has "& next" for working through a folder. -->
  <div class="row group" role="group" aria-label="Save a copy">
    <div class="split">
      <button class="btn primary save" disabled={!!blocked || app.saving} aria-busy={app.saving} data-tip={saveTip} data-tip-key="Mod+S" onclick={actions.saveCopy}>
        {#if app.saving}<span class="spin" aria-hidden="true"></span> Saving…{:else}<Icon name="save" /> Save copy{/if}
      </button>
      <Menu label="More ways to save a copy" align="right" tip="More ways to save a copy" triggerClass="primary" items={[
        { label: 'Save copy as…', hint: 'Mod+Shift+S', disabled: !!blocked, why: blocked || '', tip: 'Choose the name and folder for the copy', run: actions.saveCopyAs },
        { label: 'Save copy without hidden band data', disabled: !!blocked, why: blocked || '', tip: 'Normally a small hidden marker in the band lets Photoband re-edit the caption later.', run: actions.removeMarker },
      ]}>
        {#snippet trigger()}<Icon name="chevdown" size={12} />{/snippet}
      </Menu>
    </div>
    <button class="btn squeeze" disabled={!!blocked || app.saving} aria-label="Save copy &amp; next" data-tip={nextTip} data-tip-key="Mod+Enter" onclick={actions.saveAndNext}><span class="lbl long">Save copy &amp; next</span><span class="lbl short">Copy &amp; next</span><Icon name="next" /></button>
  </div>
  <span class="sep"></span>
  <div class="split" role="group" aria-label="Overwrite the original">
    <button class="btn squeeze overwrite" disabled={!!blocked || app.saving || !!caseC} aria-label="Overwrite original" data-tip={overwriteTip} onclick={actions.overwrite}><Icon name="overwrite" /><span class="lbl long">Overwrite original</span><span class="lbl short">Overwrite</span></button>
    <Menu label="More ways to overwrite" align="right" tip="Overwrite and go to the next photo" triggerClass="" items={[
      { label: 'Overwrite original & next', hint: 'Mod+Shift+Enter', disabled: !!(blocked || caseC), why: caseC || blocked || '', tip: 'Replace the original (a backup is kept), then open the next photo', run: actions.overwriteAndNext },
    ]}>
      {#snippet trigger()}<Icon name="chevdown" size={12} />{/snippet}
    </Menu>
  </div>
  {/if}
  <span class="sep"></span>
  {/if}
  <button class="btn ghost batchbtn" aria-label="Batch" class:on={app.view === 'batch'} aria-pressed={app.view === 'batch'} onclick={() => (app.view = app.view === 'batch' ? 'editor' : 'batch')} data-tip={app.view === 'batch' ? 'Back to the single-photo editor' : 'Caption a whole folder with one template'}><Icon name="batch" /><span class="lbl">Batch</span></button>
  <button class="btn ghost icon" data-tip="Find any command" data-tip-key="Mod+K" aria-label="Commands" aria-haspopup="dialog" onclick={() => (app.paletteOpen = true)}><Icon name="search" /></button>
  <button class="btn ghost icon" data-tip="Settings" data-tip-key="Mod+," aria-label="Settings" onclick={() => (dialogs.settingsOpen = 'general')}><Icon name="settings" /></button>
  <Menu label="Help" align="right" tip="Help, shortcuts and the save log" items={[
    { label: 'All commands…', hint: 'Mod+K', run: () => (app.paletteOpen = true) },
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
  /* split button: the action and its ▾ menu read as one control */
  .split { display: inline-flex; align-items: stretch; }
  .split > .btn { border-top-right-radius: 0; border-bottom-right-radius: 0; }
  .split :global(.wrap > .trigger) { border-top-left-radius: 0; border-bottom-left-radius: 0; margin-left: -1px; width: 24px; padding: 0; }
  .split :global(.wrap > .trigger.primary) { border-left-color: color-mix(in srgb, var(--accent-fg) 35%, var(--accent)); }
  .overwrite :global(svg) { color: var(--warn); }
  .sep { margin: 10px 6px; }
  /* pressed toggle, not a second primary button */
  .btn.on { background: var(--bg-active); border-color: var(--line-2); box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.12); }
  .spin { width: 12px; height: 12px; border: 2px solid color-mix(in srgb, var(--accent-fg) 40%, transparent); border-top-color: var(--accent-fg); border-radius: 50%; animation: sp 0.8s linear infinite; }
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
