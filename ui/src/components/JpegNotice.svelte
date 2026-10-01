<script lang="ts">
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { relToPhoto, savePreview } from '../lib/savepreview.svelte'
  import Icon from './Icon.svelte'
  let { s }: { s: PhotoSession } = $props()

  // JPEG can't be saved losslessly: say so above the photo while it is open, with the backup state
  const srcJpeg = $derived(s.meta?.info.format === 'JPEG')
  const outJpeg = $derived(app.settings.saving.outputFormat === 'jpeg' || (app.settings.saving.outputFormat === 'same' && srcJpeg))
  const backups = $derived(!!app.settings.saving.backupOriginals)
  // saving takes the photo from the untouched original backup (save preview, fetched by SavePreview)
  const pv = $derived(savePreview.path === s.path ? savePreview.pv : null)
  const original = $derived(srcJpeg && pv?.pixelSource === 'backup' ? pv.originalBackup ?? null : null)

  function turnOn() {
    app.saveSettings({ saving: { backupOriginals: true } }).then(() => app.toast('success', 'Backups are on.')).catch((e) => app.toast('error', e.message))
  }
</script>

{#if s.meta && (srcJpeg || outJpeg)}
  <div class="jn row" class:warn={!backups} role="note" aria-label="JPEG quality">
    <Icon name={backups ? 'info' : 'warn'} size={14} />
    <span class="grow">
      {#if original}An untouched original backup was found (<span class="path">{relToPhoto(s.path, original)}</span>): saving uses it, so quality doesn’t drop further.
      {:else if srcJpeg && pv?.captioned}This JPEG was captioned before and its untouched original backup wasn’t found: each save re-compresses it and may reduce quality a little.
      {:else if srcJpeg}Each save re-compresses this JPEG and may reduce quality a little.
      {:else}Copies are saved as JPEG, which re-compresses the photo and loses a little quality.{/if}
      {#if !backups}<b>Backups are off: we highly recommend turning them on</b> so an overwritten original can be restored.
      {:else if !original && pv?.backupKind === 'original'}Backups are on, so Overwrite keeps the untouched original.{/if}
    </span>
    {#if !backups}<button class="btn sm" disabled={app.saving} data-tip={app.saving ? 'Wait until the save finishes.' : 'Before each overwrite, copy the photo to the backup folder first (Settings › Saving)'} onclick={turnOn}>Turn on backups</button>{/if}
  </div>
{/if}

<style>
  .jn { gap: 8px; padding: 5px 12px; font-size: 12.5px; background: var(--info-bg); color: var(--info-fg); border-bottom: 1px solid color-mix(in srgb, var(--info-fg) 14%, transparent); }
  .jn.warn { background: var(--warn-bg); color: var(--warn-fg); border-bottom-color: color-mix(in srgb, var(--warn) 30%, transparent); }
  .jn :global(svg) { flex: none; }
  .jn.warn :global(svg) { color: var(--warn); }
  .path { font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; }
</style>
