<script lang="ts" module>
  export type Command = {
    id: string
    label: string
    group: string
    /** shortcut in tooltip notation ("Mod+S", "F1", "Y") */
    key?: string
    /** why it can't run now (the same reasons the toolbar gives); null/'' when it can */
    disabled?: string | null
    /** extra words the search matches ("print" finds Print size…) */
    keywords?: string
    /** shown after the label, e.g. "Current" */
    note?: string
    run: () => void
  }

  /** Fuzzy score of `q` against `text` (higher is better; -1 = no match). Every query letter must
   *  appear in order; whole-substring and word-start matches rank first. */
  export function fuzzyScore(q: string, text: string): number {
    q = q.trim().toLowerCase()
    if (!q) return 0
    const t = text.toLowerCase()
    const at = t.indexOf(q)
    if (at >= 0) return 1000 - at * 2 + (at === 0 || /\W/.test(t[at - 1]) ? 200 : 0) - t.length * 0.1
    let score = 0
    let ti = 0
    let prev = -2
    for (const ch of q) {
      if (ch === ' ') continue
      const i = t.indexOf(ch, ti)
      if (i < 0) return -1
      score += i === prev + 1 ? 12 : i === 0 || /\W/.test(t[i - 1]) ? 9 : 1
      score -= Math.min(4, i - ti) * 0.5
      prev = i
      ti = i + 1
    }
    return score
  }
</script>

<script lang="ts">
  import { onMount, tick } from 'svelte'
  import { shortcutLabel } from '../lib/tooltip'
  import Icon from './Icon.svelte'

  let { commands, onclose }: { commands: Command[]; onclose: () => void } = $props()

  let query = $state('')
  let active = $state(0)
  let input: HTMLInputElement | undefined = $state()
  let list: HTMLDivElement | undefined = $state()
  let flash = $state('')
  let returnTo: HTMLElement | null = null

  const results = $derived.by(() => {
    const q = query.trim()
    if (!q) return commands
    return commands
      .map((c, i) => ({ c, i, sc: Math.max(fuzzyScore(q, c.label), fuzzyScore(q, `${c.group} ${c.label} ${c.keywords || ''}`) - 50) }))
      .filter((x) => x.sc >= 0)
      // enabled commands first, then by score, then in their usual order
      .sort((a, b) => Number(!!a.c.disabled) - Number(!!b.c.disabled) || b.sc - a.sc || a.i - b.i)
      .map((x) => x.c)
  })
  $effect(() => {
    void query
    active = 0
    flash = ''
  })

  onMount(() => {
    returnTo = document.activeElement as HTMLElement | null
    input?.focus()
    return () => {
      // focus goes back where it was (unless the command moved it somewhere on purpose)
      const a = document.activeElement
      if ((!a || a === document.body || a === input) && returnTo?.isConnected) returnTo.focus({ preventScroll: true })
    }
  })

  async function scrollActive() {
    await tick()
    list?.querySelector<HTMLElement>(`[data-idx="${active}"]`)?.scrollIntoView({ block: 'nearest' })
  }

  function run(c: Command | undefined) {
    if (!c) return
    if (c.disabled) {
      flash = c.disabled
      return
    }
    onclose()
    // after the palette is gone, so focus and dialogs land where the command puts them
    setTimeout(() => c.run(), 0)
  }

  function key(e: KeyboardEvent) {
    const n = results.length
    if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      onclose()
    } else if (e.key === 'ArrowDown' || (e.key === 'n' && e.ctrlKey)) {
      e.preventDefault()
      if (n) active = (active + 1) % n
      flash = ''
      scrollActive()
    } else if (e.key === 'ArrowUp' || (e.key === 'p' && e.ctrlKey)) {
      e.preventDefault()
      if (n) active = (active - 1 + n) % n
      flash = ''
      scrollActive()
    } else if (e.key === 'PageDown' || e.key === 'PageUp') {
      e.preventDefault()
      if (n) active = Math.max(0, Math.min(n - 1, active + (e.key === 'PageDown' ? 8 : -8)))
      scrollActive()
    } else if (e.key === 'Enter') {
      e.preventDefault()
      e.stopPropagation()
      run(results[active])
    } else if (e.key === 'Tab') {
      // focus stays in the palette: the list is navigated with the arrow keys
      e.preventDefault()
      input?.focus()
    } else if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault()
      onclose()
    }
    // every other key stays here (the app's own single-key shortcuts must not fire while typing)
    e.stopPropagation()
  }

  const optId = (i: number) => `cmd-opt-${i}`
</script>

<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
<div class="scrim" onmousedown={(e) => { if (e.target === e.currentTarget) onclose() }}>
  <div class="palette" role="dialog" aria-modal="true" aria-label="Commands" tabindex="-1" onkeydown={key}>
    <div class="search row">
      <Icon name="search" size={16} />
      <input
        bind:this={input}
        bind:value={query}
        class="q"
        placeholder="Type a command…"
        role="combobox"
        aria-expanded="true"
        aria-controls="cmd-list"
        aria-autocomplete="list"
        aria-activedescendant={results.length ? optId(active) : undefined}
        aria-label="Search commands"
        spellcheck="false"
        autocomplete="off"
      />
      <kbd>Esc</kbd>
    </div>
    <div class="list scroll" id="cmd-list" role="listbox" aria-label="Commands" bind:this={list}>
      {#each results as c, i (c.id)}
        {#if !query.trim() && (i === 0 || results[i - 1].group !== c.group)}
          <div class="grp" role="presentation">{c.group}</div>
        {/if}
        <!-- options are reached with the arrow keys from the search field (aria-activedescendant), not Tab -->
        <!-- svelte-ignore a11y_click_events_have_key_events, a11y_interactive_supports_focus -->
        <div
          class="opt"
          class:on={i === active}
          class:dis={!!c.disabled}
          role="option"
          id={optId(i)}
          data-idx={i}
          aria-selected={i === active}
          aria-disabled={!!c.disabled}
          onmousemove={() => { if (active !== i) { active = i; flash = '' } }}
          onclick={() => run(c)}
        >
          <span class="lbl">{c.label}{#if c.note}<span class="note"> · {c.note}</span>{/if}</span>
          {#if c.disabled}<span class="why">{c.disabled}</span>{/if}
          {#if c.key}<kbd>{shortcutLabel(c.key)}</kbd>{/if}
        </div>
      {:else}
        <p class="empty">No command matches “{query}”.</p>
      {/each}
    </div>
    <div class="foot row" aria-live="polite">
      {#if flash}
        <Icon name="info" size={13} /><span class="grow">{flash}</span>
      {:else}
        <span><kbd>↑</kbd> <kbd>↓</kbd> to choose</span><span><kbd>Enter</kbd> to run</span>
      {/if}
    </div>
  </div>
</div>

<style>
  .scrim { position: fixed; inset: 0; z-index: 900; background: rgba(0, 0, 0, 0.18); display: flex; justify-content: center; align-items: flex-start; padding: 12vh 16px 16px; animation: fade var(--t-fast) ease-out; }
  .palette { width: min(560px, 100%); max-height: min(520px, 76vh); display: flex; flex-direction: column; background: var(--bg-2); border: 1px solid var(--line); border-radius: 12px; box-shadow: var(--shadow); overflow: hidden; animation: rise var(--t-med) ease-out; }
  @keyframes fade { from { opacity: 0; } }
  @keyframes rise { from { opacity: 0; transform: translateY(-6px) scale(0.99); } }
  .search { padding: 0 12px; gap: 8px; border-bottom: 1px solid var(--line); color: var(--fg-3); flex: none; }
  .q { flex: 1; min-width: 0; height: 46px; border: 0; background: none; outline: none; font-size: 15px; color: var(--fg); }
  .q:focus-visible { box-shadow: none; }
  .list { flex: 1; min-height: 0; padding: 4px 6px 6px; }
  .grp { padding: 10px 8px 4px; font-size: 11px; font-weight: 600; color: var(--fg-3); }
  .opt { display: flex; align-items: center; gap: 12px; min-height: 32px; padding: 4px 8px; border-radius: 6px; cursor: default; }
  .opt.on { background: color-mix(in srgb, var(--accent) 14%, var(--bg-2)); }
  .opt.on:not(.dis) { background: var(--accent); color: var(--accent-fg); }
  .opt.on:not(.dis) kbd { color: var(--accent-fg); background: transparent; border-color: rgba(255, 255, 255, 0.4); }
  .opt.on:not(.dis) .note { color: inherit; opacity: 0.8; }
  .lbl { flex: none; white-space: nowrap; }
  .note { color: var(--fg-3); }
  .dis .lbl { color: var(--fg-3); }
  .why { flex: 1; min-width: 0; font-size: 11.5px; color: var(--fg-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: right; }
  .opt kbd { margin-left: auto; flex: none; white-space: nowrap; }
  .why + kbd { margin-left: 0; }
  .empty { margin: 0; padding: 20px 12px; color: var(--fg-3); text-align: center; }
  .foot { flex: none; gap: 16px; padding: 8px 12px; border-top: 1px solid var(--line); font-size: 11.5px; color: var(--fg-3); min-height: 36px; }
</style>
