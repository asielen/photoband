<script lang="ts">
  import { app } from '../lib/store.svelte'
  import Icon from './Icon.svelte'
  import { modals } from './Modal.svelte'
</script>

<!-- Bottom centre, above the preview's status bar. While a dialog is open the toasts move to the
     top of the window (above the dialog's backdrop) so they never cover a dialog's footer buttons. -->
<div class="toasts" class:top={modals.open > 0} aria-live="polite" bind:clientHeight={modals.toastH}>
  {#each app.toasts as t (t.id)}
    <div class="toast {t.kind}" role={t.kind === 'error' ? 'alert' : 'status'}>
      <Icon name={t.kind === 'success' ? 'check' : t.kind === 'error' || t.kind === 'warn' ? 'warn' : 'info'} />
      <span class="grow text">{t.text}{#if t.file}{' '}<span class="file" data-tip={t.file}>{t.file}</span>{/if}</span>
      {#if t.action}<button class="btn sm" onclick={() => { t.action!.run(); app.dismiss(t.id) }}>{t.action.label}</button>{/if}
      <button class="btn ghost icon sm" aria-label="Dismiss" data-tip="Dismiss this message." data-tip-side="left" onclick={() => app.dismiss(t.id)}><Icon name="x" size={14} /></button>
    </div>
  {/each}
</div>

<style>
  .toasts { position: fixed; left: 50%; transform: translateX(-50%); bottom: 52px; align-items: center; display: flex; flex-direction: column; gap: 8px; z-index: 200; width: max-content; min-width: min(360px, calc(100vw - 32px)); max-width: min(480px, calc(100vw - 32px)); pointer-events: none; }
  .toast { pointer-events: auto; display: flex; align-items: center; gap: 10px; padding: 9px 10px 9px 12px; border-radius: 8px; background: var(--bg-2); border: 1px solid var(--line); box-shadow: var(--shadow); animation: in 0.16s ease-out; user-select: text; min-width: 0; }
  .text { display: flex; min-width: 0; white-space: pre-wrap; }
  .text:has(.file) { white-space: nowrap; }
  .file { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; font-weight: 500; margin-left: 0.3em; }
  .toast.success :global(svg:first-child) { color: var(--ok); }
  .toast.error { border-color: color-mix(in srgb, var(--danger) 45%, var(--line)); }
  .toast.error :global(svg:first-child) { color: var(--danger); }
  .toast.warn :global(svg:first-child) { color: var(--warn); }
  .toast.info :global(svg:first-child) { color: var(--accent-link); }
  .toasts.top { bottom: auto; top: 8px; }
  @keyframes in { from { transform: translateY(-8px); opacity: 0 } }
</style>
