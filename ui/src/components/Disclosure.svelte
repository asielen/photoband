<script lang="ts">
  // "More options" / "Advanced": a labelled show-and-hide section with a chevron, for the
  // controls most people never need. Whether it is open is remembered per viewer
  // (localStorage, best effort) under `id`. It also opens:
  //   - while `force` is set (a warning concerns a control inside it; not remembered),
  //   - when app.revealSection names it (a link elsewhere jumps to a control inside),
  //   - when a parent sets a bound `open` to true.
  import { untrack, type Snippet } from 'svelte'
  import { app } from '../lib/store.svelte'
  import { loadFlag, saveFlag } from '../lib/prefs'
  import Icon from './Icon.svelte'

  let {
    id,
    label = 'More options',
    tip = '',
    count = '',
    changed = false,
    force = false,
    open = $bindable(),
    children,
  }: {
    /** storage key and the name app.reveal() uses */
    id: string
    label?: string
    tip?: string
    /** quiet text after the label, e.g. "(3 of 12 ticked)" */
    count?: string
    /** something inside differs from the template: a dot shows it while closed */
    changed?: boolean
    /** keep it open (not remembered) while a warning points inside */
    force?: boolean
    /** bindable: a parent may open it, and can read whether it is open */
    open?: boolean
    children: Snippet
  } = $props()

  const key = untrack(() => `disclosure.${id}`)
  if (untrack(() => open) === undefined) open = loadFlag(key)
  // the user closed it while it was forced open: respect that until the reason goes away
  let dismissedForce = $state(false)
  $effect(() => {
    if (!force) dismissedForce = false
  })
  const shown = $derived(!!open || (force && !dismissedForce))
  const bodyId = $derived(`disc-${id.replace(/[^\w-]/g, '-')}`)
  let root: HTMLDivElement | undefined = $state()

  function toggle() {
    if (shown) {
      open = false
      if (force) dismissedForce = true
    } else {
      open = true
      dismissedForce = false
    }
    saveFlag(key, !!open)
  }

  $effect(() => {
    if (app.revealSection !== id) return
    app.revealSection = null
    if (!shown) {
      open = true
      saveFlag(key, true)
    }
    queueMicrotask(() => root?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }))
  })
</script>

<div class="disc" bind:this={root}>
  <button type="button" class="disclosure" aria-expanded={shown} aria-controls={bodyId} data-tip={tip || undefined} onclick={toggle}>
    <span class="chev" class:open={shown} aria-hidden="true"><Icon name="chevright" size={13} stroke={2} /></span>
    <span>{label}</span>
    {#if count}<span class="count">{count}</span>{/if}
    {#if changed && !shown}<span class="cdot" aria-label="(has changes for this photo)"></span>{/if}
  </button>
  {#if shown}
    <div class="body" id={bodyId}>
      {@render children()}
    </div>
  {/if}
</div>

<style>
  .disc { display: flex; flex-direction: column; gap: 12px; }
  .disclosure {
    display: inline-flex; align-items: center; gap: 6px; align-self: flex-start;
    min-height: 28px; padding: 0 8px 0 4px; margin-left: -4px; border: 0; border-radius: var(--radius);
    background: none; color: var(--fg-2); font-weight: 500; font-size: 12.5px; cursor: pointer;
  }
  .disclosure:hover { background: var(--bg-hover); color: var(--fg); }
  .disclosure:focus-visible { outline: none; box-shadow: var(--focus); }
  .chev { display: inline-flex; transition: transform var(--t-fast, 0.15s) ease; }
  .chev.open { transform: rotate(90deg); }
  .count { font-weight: 400; color: var(--fg-3); }
  .cdot { width: 6px; height: 6px; border-radius: 50%; background: var(--accent-link); }
  .body { display: flex; flex-direction: column; gap: 16px; animation: reveal 0.15s ease-out; }
  @keyframes reveal { from { opacity: 0; transform: translateY(-3px) } }
  @media (prefers-reduced-motion: reduce) {
    .chev { transition: none; }
    .body { animation: none; }
  }
</style>
