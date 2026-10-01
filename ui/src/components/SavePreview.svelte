<script lang="ts">
  import { post } from '../lib/api'
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { relToPhoto, savePreview, type SavePreviewInfo } from '../lib/savepreview.svelte'
  import Icon from './Icon.svelte'
  let { s }: { s: PhotoSession } = $props()

  // Where each save button writes, under the toolbar, so the choice is obvious before clicking.
  let pv = $state<SavePreviewInfo | null>(null)

  const tplName = $derived.by(() => {
    const t = app.template(s.draft.templateId)
    return t?.fromFile?.name ?? t?.name ?? ''
  })

  $effect(() => {
    // refresh for another photo, other Saving settings, another template (file-name pattern) and after each save
    const path = s.path
    const body = { path, fields: s.meta?.fields, templateName: tplName }
    void JSON.stringify(app.settings.saving)
    if (app.saving || !s.meta) return
    let stale = false
    const t = setTimeout(() => {
      // shared with JpegNotice (whether saving uses the original backup)
      const done = (r: SavePreviewInfo | null) => { if (!stale) { pv = r; savePreview.path = path; savePreview.pv = r } }
      post<SavePreviewInfo>('/api/save/preview', body).then(done).catch(() => done(null))
    }, 150)
    return () => { stale = true; clearTimeout(t) }
  })

  const dirOf = (p: string) => p.slice(0, Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')))
  const rel = (p: string) => relToPhoto(s.path, p)
  const name = $derived(s.path.slice(Math.max(s.path.lastIndexOf('/'), s.path.lastIndexOf('\\')) + 1))
  const backups = $derived(!!app.settings.saving.backupOriginals)
</script>

{#if pv}
  <div class="sp row" aria-label="Where saving writes">
    <span class="item row" data-tip={pv.copy || pv.copyError}>
      <Icon name="save" size={13} />
      <span class="k">Save copy:</span>
      {#if pv.copyError}<span class="err">{pv.copyError}</span>
      {:else}<span class="path">{rel(pv.copy)}</span><span class="faint">{dirOf(pv.copy) === dirOf(s.path) ? 'next to the original' : 'new file'}{pv.copyExists ? ' (that name exists: you’ll be asked)' : ''}</span>{/if}
    </span>
    <span class="item row" class:nobackup={!backups} data-tip={backups && pv.backup ? `Original backed up to ${pv.backup}` : 'Backups are off: the original is replaced and can’t be recovered.'}>
      <Icon name={backups ? 'overwrite' : 'warn'} size={13} />
      <span class="k">Overwrite:</span>
      <span class="path">{name}</span><span class="faint">replaced;</span>
      {#if backups && pv.backup}<span class="faint">original kept as</span><span class="path">{rel(pv.backup)}</span>{#if pv.backupExists}<span class="faint">(a backup is already there)</span>{/if}
      {:else}<span class="warn">no backup, can’t be undone</span>{/if}
    </span>
  </div>
{/if}

<style>
  .sp { gap: 18px; justify-content: flex-end; padding: 3px 12px; font-size: 12px; color: var(--fg-2); background: var(--bg-2); border-bottom: 1px solid var(--line); flex: none; min-width: 0; overflow: hidden; white-space: nowrap; }
  .item { gap: 5px; min-width: 0; flex-shrink: 1; overflow: hidden; }
  .item :global(svg) { flex: none; color: var(--fg-3); }
  .k { font-weight: 600; color: var(--fg); }
  .path { font-family: var(--mono, ui-monospace, monospace); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .warn { font-weight: 600; }
  .nobackup, .nobackup .k, .nobackup :global(svg), .nobackup .faint { color: var(--danger); }
  .err { color: var(--danger); overflow: hidden; text-overflow: ellipsis; }
</style>
