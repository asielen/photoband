<script lang="ts">
  import { proxyUrl } from '../lib/api'
  import { app } from '../lib/store.svelte'
  import { dialogs } from '../lib/dialogs.svelte'
  import { fileItems, fileMenu } from '../lib/contextmenu.svelte'
  import Icon from './Icon.svelte'
  let list: HTMLDivElement
  $effect(() => {
    const i = app.current
    const el = list?.querySelector<HTMLElement>(`[data-i="${i}"]`)
    el?.scrollIntoView({ block: 'nearest' })
  })
  // When a new set of photos opens, put keyboard focus on the current photo so Tab and the
  // arrow keys work from there (unless the user is already typing somewhere or a dialog is open).
  let focusedFor = ''
  $effect(() => {
    const key = app.photos.length ? `${app.photos.length}|${app.photos[0].path}` : ''
    const i = app.current
    if (!key || i < 0 || key === focusedFor) return
    focusedFor = key
    queueMicrotask(() => {
      if (dialogs.confirmState || dialogs.browserState || dialogs.settingsOpen || dialogs.helpOpen) return
      const a = document.activeElement as HTMLElement | null
      if (a && a !== document.body && (a.isContentEditable || a.matches('input, textarea, select') || a.closest('[role="dialog"], .insp'))) return
      list?.querySelector<HTMLElement>(`[data-i="${app.current}"]`)?.focus({ preventScroll: true })
    })
  })
  const label: Record<string, string> = { untouched: 'Not captioned yet', draft: 'Unsaved edits', saved: 'Saved', error: 'Error' }
  /** The full file name (the strip truncates it) and its status in words. */
  function tipFor(p: (typeof app.photos)[number]): string {
    const st = p.status === 'error' ? `Error: ${p.error || 'could not be opened'}` : label[p.status]
    return `${p.name}
${st}`
  }

  // one tab stop for the whole strip (the current photo); arrows move within it
  function key(e: KeyboardEvent) {
    if (e.metaKey || e.ctrlKey || e.altKey) return
    const n = app.photos.length
    let to = -1
    if (e.key === 'ArrowDown' || e.key === 'ArrowRight') to = Math.min(n - 1, app.current + 1)
    else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') to = Math.max(0, app.current - 1)
    else if (e.key === 'Home') to = 0
    else if (e.key === 'End') to = n - 1
    else if (e.key === 'PageDown') to = Math.min(n - 1, app.current + 5)
    else if (e.key === 'PageUp') to = Math.max(0, app.current - 5)
    if (to < 0) return
    e.preventDefault()
    e.stopPropagation()
    app.select(to)
    queueMicrotask(() => list?.querySelector<HTMLElement>(`[data-i="${to}"]`)?.focus())
  }
  // A thumbnail that fails to load is hidden (no broken-image icon) and tried again with a growing
  // wait (1.5 s ... 30 s) for as long as it is shown, and at once when the window regains focus
  // or the network returns: a server that was briefly unreachable never leaves it blank for good.
  const timers = new WeakMap<HTMLImageElement, ReturnType<typeof setTimeout>>()
  function retryThumb(im: HTMLImageElement) {
    clearTimeout(timers.get(im))
    if (!im.isConnected || !im.dataset.base) return
    const n = Number(im.dataset.tries || 0)
    im.src = `${im.dataset.base}${im.dataset.base.includes('?') ? '&' : '?'}retry=${n}`
  }
  function thumbFailed(e: Event) {
    const im = e.currentTarget as HTMLImageElement
    im.style.visibility = 'hidden'
    im.dataset.base ||= im.src
    const n = Number(im.dataset.tries || 0) + 1
    im.dataset.tries = String(n)
    clearTimeout(timers.get(im))
    timers.set(im, setTimeout(() => retryThumb(im), Math.min(30000, 1500 * 2 ** (n - 1))))
  }
  function thumbLoaded(e: Event) {
    const im = e.currentTarget as HTMLImageElement
    im.style.visibility = ''
    delete im.dataset.tries
    clearTimeout(timers.get(im))
  }
  function retryFailedThumbs() {
    list?.querySelectorAll<HTMLImageElement>('img[data-tries]').forEach(retryThumb)
  }
</script>

<svelte:window onfocus={retryFailedThumbs} ononline={retryFailedThumbs} />
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<div class="strip scroll" bind:this={list} role="listbox" aria-label="Photos" tabindex="-1" onkeydown={key}>
  {#each app.photos as p, i (p.path)}
    <button class="item" class:cur={i === app.current} data-i={i} role="option" aria-selected={i === app.current} tabindex={i === app.current || (app.current < 0 && i === 0) ? 0 : -1} data-tip={tipFor(p)} data-tip-side="right" onclick={() => app.select(i)}
      use:fileMenu={{ label: `Actions for ${p.name}`, items: () => fileItems(p.path) }}>
      <div class="thumb"><img src={proxyUrl(p.path, 0, true)} alt="" loading="lazy" decoding="async" onerror={thumbFailed} onload={thumbLoaded} /></div>
      <div class="meta row">
        <span class="badge {p.status}" role="img" aria-label={label[p.status]}>
          {#if p.status === 'draft'}<Icon name="pen" size={10} stroke={2.5} />{:else if p.status === 'saved'}<Icon name="check" size={10} stroke={3} />{:else if p.status === 'error'}<b aria-hidden="true">!</b>{/if}
        </span>
        <span class="name">{p.name}</span>
      </div>
    </button>
  {/each}
</div>

<style>
  .strip { height: 100%; padding: 8px; display: flex; flex-direction: column; gap: 6px; background: var(--bg-3); border-right: 1px solid var(--line); }
  .item { border: 1px solid transparent; background: none; border-radius: 8px; padding: 6px; text-align: left; cursor: default; transition: background-color var(--t-fast) ease, border-color var(--t-fast) ease; }
  .item:hover { background: var(--bg-hover); }
  .item.cur { background: color-mix(in srgb, var(--accent) 16%, var(--bg-2)); border-color: color-mix(in srgb, var(--accent) 60%, transparent); }
  .thumb { aspect-ratio: 4 / 3; background: var(--preview-bg); border-radius: 4px; display: grid; place-items: center; overflow: hidden; }
  .thumb img { max-width: 100%; max-height: 100%; object-fit: contain; display: block; }
  .meta { margin-top: 5px; gap: 6px; }
  .name { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg-2); }
  .cur .name { color: var(--fg); }
  /* status is a shape, not only a colour: pencil = unsaved edits, check = saved, ! = error, nothing = untouched */
  .badge { width: 14px; height: 14px; flex: none; display: inline-grid; place-items: center; border-radius: 3px; color: var(--bg-2); font-size: 11px; line-height: 1; }
  .badge.untouched { width: 0; margin-right: -6px; }
  .badge.draft { background: var(--warn); border-radius: 3px; }
  .badge.saved { background: var(--ok); border-radius: 50%; }
  .badge.error { background: var(--danger); clip-path: polygon(50% 0, 100% 100%, 0 100%); width: 15px; padding-top: 3px; }
  .badge b { font-weight: 800; }
</style>
