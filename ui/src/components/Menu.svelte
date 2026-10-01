<script lang="ts">
  import type { Snippet } from 'svelte'
  import { shortcutLabel } from '../lib/tooltip'
  let { label, items, trigger, align = 'left', tip = '', tipKey = '', triggerClass = 'ghost' }: {
    label: string
    /** hint: a shortcut such as "Mod+Shift+O" (shown as ⌘⇧O / Ctrl+Shift+O); why: shown as the tooltip when disabled */
    items: { label: string; hint?: string; disabled?: boolean; why?: string; tip?: string; run: () => void; sep?: boolean }[]
    trigger: Snippet
    align?: 'left' | 'right'
    /** the trigger's tooltip (defaults to the label) */
    tip?: string
    tipKey?: string
    triggerClass?: string
  } = $props()
  let open = $state(false)
  let root: HTMLDivElement
  let pos = $state('')
  // position: fixed so the menu isn't clipped by a scrolling parent (e.g. a dialog body);
  // it opens upward when there isn't room below.
  function place() {
    const r = root.querySelector('.trigger')!.getBoundingClientRect()
    const need = Math.min(40 + items.length * 32, 360)
    const up = window.innerHeight - r.bottom < need && r.top > window.innerHeight - r.bottom
    const v = up ? `bottom:${window.innerHeight - r.top + 4}px` : `top:${r.bottom + 4}px`
    const h = align === 'right' ? `right:${window.innerWidth - r.right}px` : `left:${r.left}px`
    pos = `${v};${h}`
  }
  $effect(() => {
    if (!open) return
    const f = () => place()
    document.addEventListener('scroll', f, true)
    return () => document.removeEventListener('scroll', f, true)
  })
  function toggle() {
    open = !open
    if (open) {
      place()
      queueMicrotask(() => root.querySelector<HTMLButtonElement>('.menu button:not(:disabled)')?.focus())
    }
  }
  function outside(e: MouseEvent) {
    if (open && root && !root.contains(e.target as Node)) open = false
  }
  function key(e: KeyboardEvent) {
    if (!open) return
    const btns = [...root.querySelectorAll<HTMLButtonElement>('.menu button:not(:disabled)')]
    const i = btns.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'Escape') { open = false; e.stopPropagation(); e.preventDefault(); root.querySelector<HTMLButtonElement>('.trigger')?.focus() }
    if (e.key === 'ArrowDown') { btns[(i + 1) % btns.length]?.focus(); e.preventDefault(); e.stopPropagation() }
    if (e.key === 'ArrowUp') { btns[(i - 1 + btns.length) % btns.length]?.focus(); e.preventDefault(); e.stopPropagation() }
    if (e.key === 'Tab') open = false
  }
</script>

<svelte:window onmousedown={outside} onresize={() => (open = false)} />
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="wrap" bind:this={root} onkeydown={key}>
  <button class="btn {triggerClass} trigger" aria-haspopup="menu" aria-expanded={open} aria-label={label} data-tip={open ? undefined : tip || label} data-tip-key={open ? undefined : tipKey || undefined} onclick={toggle}>
    {@render trigger()}
  </button>
  {#if open}
    <div class="menu" role="menu" style={pos}>
      {#each items as it}
        {#if it.sep}<div class="msep"></div>{/if}
        <button role="menuitem" disabled={it.disabled} data-tip={(it.disabled && it.why) || it.tip || undefined} data-tip-side={align === 'right' ? 'left' : 'right'} onclick={() => { open = false; root.querySelector<HTMLButtonElement>('.trigger')?.focus(); it.run() }}>
          <span>{it.label}</span>{#if it.hint}<kbd>{shortcutLabel(it.hint)}</kbd>{/if}
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .wrap { position: relative; display: inline-flex; }
  .menu { position: fixed; min-width: 260px; width: max-content; max-width: min(420px, calc(100vw - 32px)); background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); padding: 4px; z-index: 50; animation: pop var(--t-fast) ease-out; }
  @keyframes pop { from { opacity: 0; transform: translateY(-3px); } }
  .menu button span { white-space: nowrap; }
  .menu kbd { white-space: nowrap; flex: none; }
  .menu button { display: flex; width: 100%; min-height: 28px; justify-content: space-between; align-items: center; gap: 16px; border: 0; background: none; padding: 6px 10px; border-radius: 5px; text-align: left; cursor: default; }
  .menu button:hover:not(:disabled), .menu button:focus-visible { background: var(--accent); color: var(--accent-fg); box-shadow: none; }
  .menu button:hover:not(:disabled) kbd, .menu button:focus-visible kbd { color: var(--accent-fg); background: transparent; border-color: rgba(255,255,255,.4); }
  .menu button:disabled { opacity: 0.4; }
  .msep { height: 1px; background: var(--line); margin: 4px 6px; }
</style>
