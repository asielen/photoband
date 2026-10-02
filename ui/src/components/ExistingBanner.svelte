<script lang="ts">
  import { app, type PhotoSession } from '../lib/store.svelte'
  import Icon from './Icon.svelte'
  import Segmented from './Segmented.svelte'

  let { s, tool = $bindable('pan') }: { s: PhotoSession; tool?: 'pan' | 'edge' | 'brush-add' | 'brush-remove' } = $props()
  const ex = $derived(s.existing)
  const conf = $derived(ex?.confidence != null ? Math.round(ex.confidence * 100) : null)
  const active = $derived(s.draft.mode !== 'band')
  // "Ignore"/"Dismiss" is remembered on the photo (Save then doesn't ask about the old band)
  const dismissed = $derived(s.existingIgnored)
  const details = $derived.by(() => {
    if (!ex) return ''
    const parts: string[] = []
    if (ex.source === 'marker') parts.push('exact photo edge known')
    if (conf != null) parts.push(`${conf}% sure`)
    // case C: writing was seen but couldn't be read; case B: a band without readable text
    if (!ex.hasText) parts.push(ex.case === 'C' ? 'the words couldn’t be read' : 'no text found in it')
    return parts.join(' · ')
  })
  $effect(() => {
    if (s.draft.mode !== 'erase' && tool.startsWith('brush')) tool = 'pan'
    if (s.draft.mode === 'band' && tool === 'edge') tool = 'pan'
  })
  // blocks the backend classified as paper backprint / lab logo (role "other")
  const otherText = $derived.by(() => {
    const bl = (ex?.blocks ?? []) as { role?: string; lines: { text: string }[] }[]
    return bl.filter((b) => b.role === 'other').map((b) => b.lines.map((l) => l.text).join(' ').trim()).filter(Boolean)
  })
  const overCount = $derived(ex?.textOverPhoto?.length ?? 0)
  // a draft that edits an old caption the (finished) check didn't find in this version of the file
  const orphaned = $derived(!s.existingLoading && !!ex && s.draft.mode !== 'band' && !!app.geometryProblem(s))

  function toggleEdge(e: MouseEvent) {
    tool = tool === 'edge' ? 'pan' : 'edge'
    // arrow-key nudging is handled globally only when focus is on the page or the
    // preview, not on this button: hand focus to the Before canvas (or the page)
    const btn = e.currentTarget as HTMLElement | null
    if (tool === 'edge') {
      const cv = document.querySelector('canvas[aria-label^="Before"]') as HTMLElement | null
      cv?.focus({ preventScroll: true })
      if (document.activeElement === btn) btn?.blur()
    }
  }
</script>

{#if s.existingLoading}
  <div class="banner quiet row"><span class="spin"></span> Checking for an existing caption… Saving waits until this is done.</div>
{:else if orphaned}
  <div class="banner warn row">
    <Icon name="warn" />
    <div class="grow"><b>The old caption wasn’t found.</b> This photo’s unsaved edits replace an old caption, but it couldn’t be found in this version of the file, so saving is blocked.</div>
    <button class="btn sm" data-tip="Look for the old caption in this file again" onclick={() => app.recheckExisting(s)}>Check again</button>
    <button class="btn sm ghost" data-tip="Drop the edits to the old caption and add a new band below the photo instead" onclick={() => { tool = 'pan'; app.leaveExisting(s) }}>Add a new band instead</button>
  </div>
{:else if ex && ex.case && !dismissed}
  {#if ex.case === 'A'}
    <div class="banner info row">
      <Icon name="check" />
      <div class="grow">
        <b>Captioned by Photoband.</b>
        {#if active}Editing the saved caption: saving replaces the band instead of adding a second one.{:else}This photo already has a Photoband band.{/if}
        {#if ex.source === 'marker+payload'}<span class="faint" data-tip="The file's metadata had been removed, so the caption was read back from data hidden in the band."> Restored from the band itself.</span>{/if}
        <!-- JPEG quality is explained above the photo (JpegNotice: it knows whether the original backup is used) -->
      </div>
      {#if active}
        <button class="btn sm" data-tip="Replace the saved caption text with the template's text" onclick={() => app.resetAllBlocks(s)}>Start from template</button>
        <button class="btn sm ghost" onclick={() => app.leaveExisting(s)} data-tip="Treat the whole image, band included, as the photo">Add a new band</button>
      {:else}
        <button class="btn sm primary" data-tip="Open the saved caption for editing. Saving replaces the band instead of adding a second one." onclick={() => app.editExisting(s)}>Edit existing caption</button>
        {#if !s.draft.keepBand}<button class="btn sm ghost" onclick={() => app.keepExistingBand(s)} data-tip="Treat the whole image, band included, as the photo">Add a new band</button>{/if}
      {/if}
    </div>
  {:else if ex.case === 'B' || ex.case === 'C'}
    <div class="banner stacked {ex.case === 'C' ? 'warn' : 'info'}">
      <div class="row msgrow">
        <Icon name={ex.case === 'C' ? 'pen' : 'text'} />
        <div class="grow">
          <b>{ex.case === 'C' ? 'Handwriting or printing on the photo’s border' : 'This photo already has a caption band'}</b>
          {#if details}<span class="faint"> · {details}</span>{/if}
          {#if ex.case === 'C'}<div class="faint small">{#if app.settings.saving.allowOverwriteHandwritten}Writing on an original print is part of its history: saving a copy keeps the original as it is.{:else}Writing on an original print is part of its history, so Photoband won’t overwrite this scan. Saving makes a captioned copy and leaves the original as it is (Settings › Saving can allow overwriting).{/if}{ex.hasText ? ' The words read from it are kept in the copy’s photo information.' : ''}</div>{/if}
          {#if overCount}<div class="faint small">Text printed over the photo ({overCount === 1 ? 'outlined in orange' : `${overCount} places, outlined in orange`} in Before) is flagged only and never erased.</div>{/if}
          {#if otherText.length}<div class="faint small">Not treated as caption (paper backprint or lab logo): {otherText.map((t) => `“${t}”`).join(', ')}.</div>{/if}
        </div>
      </div>
      <div class="row tools">
      {#if !active}
        {#if ex.hasText}<button class="btn sm primary" data-tip="Use the text read from the old caption. Check the words underlined as uncertain." onclick={() => app.useExisting(s, 'recognized', ex.case === 'C' ? 'erase' : 'rebuild')}>Use recognized text</button>{/if}
        <button class="btn sm" data-tip={ex.case === 'C' ? 'On the copy, erase the writing and put the template’s caption in its place. The original scan is not changed.' : 'Remove the old band and add a new one with the template’s caption'} onclick={() => app.useExisting(s, 'template', ex.case === 'C' ? 'erase' : 'rebuild')}>{ex.case === 'C' ? 'Erase it, use template' : 'Replace with template'}</button>
        <button class="btn sm ghost" data-tip={ex.case === 'C' ? 'Leave the writing as part of the photo and add a new caption band below it' : 'Keep the old band as part of the photo and add a new band below it'} onclick={() => (s.existingIgnored = true)}>{ex.case === 'C' ? 'Keep the writing' : 'Keep it'}</button>
      {:else}
        <Segmented small value={s.draft.mode} label="Replacement mode" options={[{ value: 'rebuild', label: 'Rebuild band', tip: 'Crop to the photo and add a new band' }, { value: 'erase', label: 'Erase in place', tip: 'Keep the original band and paper; erase the old text and write the new text there' }]} onchange={(v) => app.useExisting(s, s.draft.existingChoice || 'template', v as any)} />
        <span class="sep"></span>
        <button class="btn sm" class:on={tool === 'edge'} aria-pressed={tool === 'edge'} data-tip="Adjust where the photo ends: drag an edge, or use the arrow keys (Shift for 10 px)" onclick={toggleEdge}><Icon name="move" size={14} /> Edge</button>
        {#if s.draft.mode === 'erase'}
          <button class="btn sm" class:on={tool === 'brush-add'} aria-pressed={tool === 'brush-add'} data-tip="Paint over marks that should be erased too" onclick={() => (tool = tool === 'brush-add' ? 'pan' : 'brush-add')}><Icon name="brush" size={14} /> Erase more</button>
          <button class="btn sm" class:on={tool === 'brush-remove'} aria-pressed={tool === 'brush-remove'} data-tip="Paint over areas that must not be erased" onclick={() => (tool = tool === 'brush-remove' ? 'pan' : 'brush-remove')}><Icon name="eraser" size={14} /> Keep</button>
        {/if}
        <button class="btn sm ghost" data-tip="Undo this choice and pick again. The old caption is left as it is." onclick={() => { tool = 'pan'; app.leaveExisting(s) }}>Cancel</button>
      {/if}
      </div>
    </div>
    {#if active}
      <div class="hint faint">Before shows the detected photo edge (cyan) and text lines (pink). Hold <kbd>Space</kbd> to flip After to the original.{tool === 'edge' ? ' Click an edge to select it; drag it or use the arrow keys.' : ''}</div>
    {/if}
  {:else if ex.case === 'D'}
    <div class="banner warn row">
      <Icon name="warn" />
      <div class="grow"><b>Text printed over the photo?</b> <span class="faint">(outlined in orange in Before). A best-effort check: it can miss some text. It is flagged only and is never erased.</span></div>
      <button class="btn sm ghost" data-tip="Hide this note. The outline stays only in Before and is never saved." onclick={() => (s.existingIgnored = true)}>Dismiss</button>
    </div>
  {/if}
{/if}
{#if ex?.warnings?.length && !dismissed}
  {#each ex.warnings.filter((w) => !w.startsWith('Text printed over')) as w}
    <div class="banner quiet row"><Icon name="info" size={14} /> {w}</div>
  {/each}
{/if}

<style>
  .banner { padding: 8px 12px; gap: 8px 12px; border-bottom: 1px solid var(--line); font-size: 12.5px; flex-wrap: wrap; }
  .banner.info { background: var(--info-bg); color: var(--info-fg); }
  .banner.warn { background: var(--warn-bg); color: var(--warn-fg); }
  .banner.quiet { background: var(--bg-2); color: var(--fg-2); }
  /* message on its own row, tools below it: nothing collapses at narrow widths */
  .banner.stacked { display: flex; flex-wrap: wrap; row-gap: 6px; }
  .banner.stacked .msgrow { flex-basis: 100%; gap: 10px; align-items: flex-start; }
  .banner.stacked .tools { flex-wrap: wrap; gap: 6px; padding-left: 26px; }
  .banner .faint { color: inherit; opacity: 0.75; }
  .small { font-size: 12px; }
  .btn.on { background: var(--bg-active); border-color: var(--line-2); box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.12); }
  .hint { padding: 4px 12px; font-size: 12px; background: var(--bg-2); border-bottom: 1px solid var(--line); }
  .spin { width: 12px; height: 12px; border: 2px solid var(--line-2); border-top-color: var(--accent); border-radius: 50%; animation: sp 0.8s linear infinite; }
  @keyframes sp { to { transform: rotate(360deg) } }
</style>
