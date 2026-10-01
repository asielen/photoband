<script lang="ts">
  type Option = { value: string; label: string; /** tooltip */ tip?: string; /** older name for tip */ title?: string; disabled?: boolean; /** why it is disabled */ why?: string }
  let { value, options, onchange, label = '', small = false }: { value: string; options: Option[]; onchange: (v: string) => void; label?: string; small?: boolean } = $props()
  let root: HTMLDivElement
  const tabStop = $derived(options.some((o) => o.value === value && !o.disabled) ? value : options.find((o) => !o.disabled)?.value)

  // Radio-group keyboard pattern: arrows move the selection (and focus) within the group.
  // stopPropagation keeps the app's global arrow-key photo navigation from firing too.
  function key(e: KeyboardEvent) {
    const fwd = e.key === 'ArrowRight' || e.key === 'ArrowDown'
    const back = e.key === 'ArrowLeft' || e.key === 'ArrowUp'
    const home = e.key === 'Home'
    const end = e.key === 'End'
    if (!fwd && !back && !home && !end) return
    e.preventDefault()
    e.stopPropagation()
    const en = options.filter((o) => !o.disabled)
    if (!en.length) return
    let i = en.findIndex((o) => o.value === value)
    if (home) i = 0
    else if (end) i = en.length - 1
    else i = (i + (fwd ? 1 : -1) + en.length) % en.length
    const next = en[i]
    if (next.value !== value) onchange(next.value)
    const k = options.indexOf(next)
    root.querySelectorAll<HTMLButtonElement>('button')[k]?.focus()
  }
</script>

<!-- svelte-ignore a11y_interactive_supports_focus -->
<div class="seg" class:small role="radiogroup" aria-label={label} bind:this={root} onkeydown={key}>
  {#each options as o}
    <button type="button" role="radio" aria-checked={value === o.value} class:on={value === o.value} tabindex={o.value === tabStop ? 0 : -1} data-tip={(o.disabled && o.why) || o.tip || o.title || undefined} disabled={o.disabled} onclick={() => onchange(o.value)}>{o.label}</button>
  {/each}
</div>

<style>
  /* iOS-style: the chosen option is a raised chip on a sunken track (calmer than a filled button) */
  .seg { display: inline-flex; gap: 2px; padding: 2px; border-radius: calc(var(--radius) + 2px); background: var(--seg-track); justify-self: start; align-self: flex-start; width: max-content; max-width: 100%; flex: none; }
  button { border: 0; border-radius: var(--radius); background: none; padding: 0 10px; height: 24px; cursor: pointer; color: var(--fg-2); font-size: 12px; font-weight: 500; white-space: nowrap; transition: background-color var(--t-fast) ease, color var(--t-fast) ease; }
  button:hover:not(:disabled):not(.on) { background: var(--bg-hover); color: var(--fg); }
  button:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
  .small button { padding: 0 8px; height: 22px; }
  button.on { background: var(--bg-2); color: var(--fg); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.14), 0 0 0 0.5px rgba(0, 0, 0, 0.08); }
  button.on:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); }
  button:disabled { opacity: 0.4; cursor: default; }
</style>
