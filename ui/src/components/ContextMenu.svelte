<script lang="ts">
  import { closeContextMenu, contextMenu } from '../lib/contextmenu.svelte'
  let el = $state<HTMLDivElement | null>(null)
  const open = $derived(!!contextMenu?.items.length)
  // keep the menu inside the window
  let pos = $state('')
  $effect(() => {
    if (!open || !el) return
    const r = el.getBoundingClientRect()
    const x = Math.min(contextMenu!.x, window.innerWidth - r.width - 4)
    const y = contextMenu!.y + r.height > window.innerHeight ? Math.max(4, contextMenu!.y - r.height) : contextMenu!.y
    pos = `left:${Math.max(4, x)}px;top:${y}px`
    queueMicrotask(() => el?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus())
  })
  function key(e: KeyboardEvent) {
    const btns = [...el!.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
    const i = btns.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'Escape') closeContextMenu(true)
    else if (e.key === 'ArrowDown') btns[(i + 1) % btns.length]?.focus()
    else if (e.key === 'ArrowUp') btns[(i - 1 + btns.length) % btns.length]?.focus()
    else if (e.key === 'Tab') closeContextMenu()
    else return
    e.preventDefault()
    e.stopPropagation()
  }
  function outside(e: MouseEvent) {
    if (open && el && !el.contains(e.target as Node)) closeContextMenu()
  }
</script>

<svelte:window onmousedown={outside} onresize={() => closeContextMenu()} onblur={() => closeContextMenu()} />
{#if open}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div class="menu ctx" role="menu" aria-label={contextMenu!.label} tabindex="-1" bind:this={el} style={pos} onkeydown={key} oncontextmenu={(e) => e.preventDefault()}>
    {#each contextMenu!.items as it}
      {#if it.sep}<div class="sepline" role="separator"></div>{/if}
      <button role="menuitem" disabled={it.disabled} onclick={() => { closeContextMenu(true); it.run() }}>{it.label}</button>
    {/each}
  </div>
{/if}

<style>
  .ctx { position: fixed; z-index: 1000; min-width: 180px; padding: 4px; background: var(--bg-1, var(--bg)); border: 1px solid var(--line-2, var(--line)); border-radius: 8px; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.18); display: flex; flex-direction: column; }
  .ctx button { text-align: left; padding: 6px 10px; border: 0; background: transparent; color: var(--fg); border-radius: 5px; font: inherit; font-size: 13px; cursor: pointer; }
  .ctx button:hover:not(:disabled), .ctx button:focus-visible { background: var(--bg-hover); outline: none; }
  .ctx button:disabled { color: var(--fg-3); cursor: default; }
  .sepline { height: 1px; margin: 4px 6px; background: var(--line); }
</style>
