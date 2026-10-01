<script lang="ts">
  import { app, type PhotoSession } from '../lib/store.svelte'
  import type { Warning } from '../lib/types'
  import Icon from './Icon.svelte'
  let { s }: { s: PhotoSession } = $props()

  // overflow/glyph problems change what is printed; the rest are cautions or plain information
  type Level = 'error' | 'warn' | 'info'
  const LEVEL: Record<Warning['kind'], Level> = {
    overflow: 'error', glyph: 'error', small: 'warn', font: 'warn', dpi: 'warn', color: 'warn', metadata: 'info', info: 'info',
  }
  const RANK: Record<Level, number> = { error: 0, warn: 1, info: 2 }
  const level = (w: Warning): Level => LEVEL[w.kind] ?? 'warn'

  /** The Inspector control a warning is about (so it can be shown, even behind "More options"). */
  type Target = { tab: 'style' | 'layout'; section: string | null; label: string }
  function target(w: Warning): Target | null {
    if (w.kind === 'overflow' || (w.kind === 'small' && !w.block)) return { tab: 'layout', section: 'layout-more', label: 'Band settings' }
    if (w.kind === 'font' && /small capitals|no bold|no italic/.test(w.message)) return { tab: 'style', section: 'style-more', label: 'Font settings' }
    if (w.kind === 'font' || w.kind === 'glyph' || (w.kind === 'small' && w.block)) return { tab: 'style', section: null, label: 'Font settings' }
    if (w.kind === 'color') return w.block ? { tab: 'style', section: null, label: 'Text colour' } : { tab: 'layout', section: null, label: 'Band colour' }
    return null
  }

  const ws = $derived.by(() => {
    void s.layout
    void s.meta
    // most serious first (stable within a level)
    return app.warnings(s).map((w, i) => ({ w, i })).sort((a, b) => RANK[level(a.w)] - RANK[level(b.w)] || a.i - b.i).map((x) => x.w)
  })
  const top = $derived<Level>(ws.length ? level(ws[0]) : 'info')
  let expanded = $state(false)
</script>

{#snippet fix(w: Warning)}
  {@const t = target(w)}
  {#if t}<button class="btn ghost sm fix" data-tip="Show the setting this is about" data-tip-side="top" onclick={() => app.reveal(t.tab, t.section)}>{t.label}</button>{/if}
{/snippet}

{#if ws.length}
  <div class="strip {top}" role="status">
    <div class="row">
      <span class="ic {level(ws[0])}"><Icon name={level(ws[0]) === 'info' ? 'info' : 'warn'} size={14} /></span>
      <span class="grow msg">{ws[0].message}</span>
      {@render fix(ws[0])}
      {#if ws.length > 1}<button class="btn ghost sm" aria-expanded={expanded} data-tip={expanded ? 'Show only the most important note' : 'Show every note about this photo'} data-tip-side="top" onclick={() => (expanded = !expanded)}>{expanded ? 'Less' : `+${ws.length - 1} more`}</button>{/if}
    </div>
    {#if expanded}
      {#each ws.slice(1) as w}
        <div class="row more"><span class="ic {level(w)}"><Icon name={level(w) === 'info' ? 'info' : 'warn'} size={12} /></span><span class="msg grow">{w.message}</span>{@render fix(w)}</div>
      {/each}
    {/if}
  </div>
{/if}

<style>
  .strip { padding: 4px 12px; font-size: 12.5px; max-height: 40vh; overflow: auto; border-top: 1px solid var(--line); }
  .strip.info { background: var(--bg-2); color: var(--fg-2); }
  .strip.warn { background: var(--warn-bg); color: var(--warn-fg); border-top-color: color-mix(in srgb, var(--warn) 30%, transparent); }
  .strip.error { background: var(--err-bg); color: var(--err-fg); border-top-color: color-mix(in srgb, var(--danger) 30%, transparent); }
  .ic { display: inline-flex; flex: none; }
  .ic.info { color: var(--fg-3); }
  .ic.warn { color: var(--warn); }
  .ic.error { color: var(--danger); }
  .more { padding-left: 4px; margin-top: 2px; }
  .msg { user-select: text; }
  .strip :global(.btn) { color: inherit; }
  .fix { text-decoration: underline; text-underline-offset: 2px; text-decoration-color: color-mix(in srgb, currentColor 40%, transparent); }
</style>
