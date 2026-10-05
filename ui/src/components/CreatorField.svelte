<script lang="ts">
  // The photographers, one chip each: a name is never split (a comma or semicolon in it is part of
  // the name). Enter adds the name typed; Backspace on an empty field removes the last one.
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { cleanText } from '../lib/metaedits'

  let { s, id }: { s: PhotoSession; id?: string } = $props()

  const list = $derived(app.creators(s))
  let text = $state('')
  let input: HTMLInputElement

  function add() {
    const name = Array.from(cleanText(text, false).trim()).slice(0, 2000).join('')
    if (name && !list.some((x) => x.toLowerCase() === name.toLowerCase())) app.setCreators(s, [...list, name])
    text = ''
  }
  function remove(i: number) {
    app.setCreators(s, list.filter((_, j) => j !== i))
    input?.focus()
  }
  function key(e: KeyboardEvent) {
    if (e.isComposing) return
    if (e.key === 'Enter') {
      e.preventDefault()
      add()
    } else if (e.key === 'Backspace' && !text && list.length) {
      e.preventDefault()
      remove(list.length - 1)
    }
  }
</script>

<!-- svelte-ignore a11y_click_events_have_key_events -->
<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="chips" onclick={() => input?.focus()}>
  {#each list as name, i (name)}
    <span class="chip">{name}<button type="button" class="x" aria-label={`Remove photographer ${name}`} onclick={(e) => { e.stopPropagation(); remove(i) }}>×</button></span>
  {/each}
  <input bind:this={input} {id} class="in" maxlength="2000" bind:value={text} onkeydown={key} onblur={add}
    placeholder={list.length ? 'Add…' : 'Add a photographer'} aria-label="Add a photographer" />
</div>

<style>
  .chips { display: flex; flex-wrap: wrap; gap: 4px; padding: 4px; min-height: 30px; border: 1px solid var(--line-2); border-radius: var(--radius); background: var(--bg-2); cursor: text; }
  .chips:focus-within { border-color: var(--accent-link); box-shadow: var(--focus); }
  .chip { display: inline-flex; align-items: center; gap: 2px; height: 22px; padding: 0 2px 0 8px; border-radius: 11px; background: var(--bg-3); font-size: 12px; max-width: 100%; overflow-wrap: anywhere; }
  .x { border: 0; background: none; width: 18px; height: 18px; border-radius: 50%; cursor: pointer; color: var(--fg-3); font-size: 13px; line-height: 1; padding: 0; }
  .x:hover { background: var(--bg-hover); color: var(--fg); }
  .in { flex: 1; min-width: 90px; border: 0; background: none; outline: none; font: inherit; font-size: 12.5px; color: var(--fg); padding: 0 4px; height: 22px; }
</style>
