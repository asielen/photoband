<script lang="ts">
  // Keywords as chips: type and press Enter or a comma to add, Backspace on an empty field removes
  // the last one, pasting a list splits it. photokin's markers show apart (they are kept, not printed).
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { addKeywords, cleanText, isMarkerKeyword, splitKeywords } from '../lib/metaedits'

  let { s }: { s: PhotoSession } = $props()

  const list = $derived(app.keywords(s))
  let text = $state('')
  let input: HTMLInputElement

  const MAX = 500
  function add(raw: string) {
    const parts = splitKeywords(cleanText(raw, true)).map((k) => k.slice(0, 200))
    if (parts.length) {
      const next = addKeywords(list, parts)
      if (next.length > MAX) app.toast('warn', `A photo can have at most ${MAX} keywords.`)
      app.setKeywords(s, next.slice(0, MAX))
    }
    text = ''
  }
  function remove(k: string) {
    app.setKeywords(s, list.filter((x) => x !== k))
    input?.focus()
  }
  function key(e: KeyboardEvent) {
    if (e.isComposing) return   // an input method is still composing
    if (e.key === 'Enter' || e.key === ',' || e.key === ';') {
      e.preventDefault()
      add(text)
    } else if (e.key === 'Backspace' && !text && list.length) {
      e.preventDefault()
      remove(list[list.length - 1])
    }
  }
  function paste(e: ClipboardEvent) {
    const t = e.clipboardData?.getData('text') ?? ''
    if (/[,;\n]/.test(t)) {
      e.preventDefault()
      add(text + t)
    }
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="chips" onclick={() => input?.focus()}>
  {#each list as k (k)}
    <span class="chip" class:marker={isMarkerKeyword(k)} data-tip={isMarkerKeyword(k) ? 'A marker photokin added. Kept in the file; {keywords} leaves it out of captions.' : undefined}>
      {k}<button type="button" class="x" aria-label={`Remove keyword ${k}`} onclick={(e) => { e.stopPropagation(); remove(k) }}>×</button>
    </span>
  {/each}
  <input bind:this={input} class="in" maxlength="2000" bind:value={text} onkeydown={key} onpaste={paste} onblur={() => add(text)}
    placeholder={list.length ? 'Add…' : 'Add keywords, separated by commas'} aria-label="Add keywords" />
</div>

<style>
  .chips { display: flex; flex-wrap: wrap; gap: 4px; padding: 4px; min-height: 30px; border: 1px solid var(--line-2); border-radius: var(--radius); background: var(--bg-2); cursor: text; }
  .chips:focus-within { border-color: var(--accent-link); box-shadow: var(--focus); }
  .chip { display: inline-flex; align-items: center; gap: 2px; height: 22px; padding: 0 2px 0 8px; border-radius: 11px; background: var(--bg-3); font-size: 12px; max-width: 100%; overflow-wrap: anywhere; }
  .chip.marker { background: transparent; border: 1px dashed var(--line-2); color: var(--fg-3); }
  .x { border: 0; background: none; width: 18px; height: 18px; border-radius: 50%; cursor: pointer; color: var(--fg-3); font-size: 13px; line-height: 1; padding: 0; }
  .x:hover { background: var(--bg-hover); color: var(--fg); }
  .in { flex: 1; min-width: 90px; border: 0; background: none; outline: none; font: inherit; font-size: 12.5px; color: var(--fg); padding: 0 4px; height: 22px; }
</style>
