<script lang="ts">
  import { post } from '../lib/api'
  import { htmlToMarkup, markupToHtml, escapePlain } from '../lib/markup'
  import { app, type PhotoSession } from '../lib/store.svelte'
  import type { Block } from '../lib/types'
  import Icon from './Icon.svelte'

  let { s, block, lowConfidence = [] }: { s: PhotoSession; block: Block; lowConfidence?: string[] } = $props()

  let el: HTMLDivElement
  let lastEmitted = ''
  let focused = $state(false)
  let insertOpen = $state(false)
  let insertValues = $state<Record<string, string>>({})
  let insRoot: HTMLDivElement
  let pop: HTMLDivElement | undefined = $state()

  const text = $derived(app.blockText(s, block.id))
  const custom = $derived(app.isCustom(s, block.id))
  const empty = $derived(!text.trim())
  const res = $derived(s.resolved[block.id])

  /** Put the caret after the last character. */
  function caretToEnd() {
    const sel = window.getSelection()
    if (!sel || !el) return
    const r = document.createRange()
    r.selectNodeContents(el)
    r.collapse(false)
    sel.removeAllRanges()
    sel.addRange(r)
  }

  $effect(() => {
    const t = text
    if (!el) return
    if (t !== lastEmitted || !focused) {
      if (htmlToMarkup(el) !== t) {
        el.innerHTML = markupToHtml(t, custom ? lowConfidence : [])
        // undo/redo or a photo change while typing: keep the caret usable (at the end), not at the start
        if (document.activeElement === el) caretToEnd()
      }
      lastEmitted = t
    }
  })

  $effect(() => {
    if (app.focusBlock === block.id && el) {
      el.focus()
      caretToEnd()
      el.scrollIntoView({ block: 'nearest' })
      app.focusBlock = null
    }
  })

  const placeholder = $derived.by(() => {
    const empties = res?.empty_tokens ?? []
    // a custom block no longer follows the photo's information, so what is missing there is irrelevant
    if (custom || !empties.length) return 'Empty — this line is left out of the band'
    return `Empty — this photo has no ${block.name.toLowerCase()}`
  })

  // plain names for the photo information the Insert menu offers
  const LABELS: Record<string, string> = {
    title: 'Title', caption: 'Description', creator: 'Photographer', date: 'Date', 'date:yyyy': 'Year',
    today: 'Today’s date', names: 'Names, left to right', 'names:rows': 'Names, row by row', 'names.count': 'Number of people',
    'faces.unnamed_count': 'Faces without a name', location: 'Place', city: 'City', state: 'State or province',
    country: 'Country', keywords: 'Keywords', filename: 'File name', stem: 'File name without extension', folder: 'Folder',
  }
  const label = (k: string) => LABELS[k] ?? k

  function onInput() {
    const m = htmlToMarkup(el)
    lastEmitted = m
    app.setBlockText(s, block.id, m)
  }

  function fmt(cmd: 'bold' | 'italic') {
    el.focus()
    document.execCommand(cmd)
    onInput()
  }

  function onKey(e: KeyboardEvent) {
    const mod = e.metaKey || e.ctrlKey
    if (mod && e.key.toLowerCase() === 'b') {
      e.preventDefault()
      fmt('bold')
    } else if (mod && e.key.toLowerCase() === 'i') {
      e.preventDefault()
      fmt('italic')
    } else if (mod && e.key.toLowerCase() === 'z') {
      // the photo's own undo history (text, style and layout together)
      e.preventDefault()
      e.stopPropagation()
      if (e.shiftKey) app.redo(s)
      else app.undo(s)
    } else if (mod && e.key.toLowerCase() === 'y') {
      e.preventDefault()
      e.stopPropagation()
      app.redo(s)
    } else if (e.key === 'Enter' && !e.shiftKey && !mod) {
      // plain line break (no <div> soup)
      e.preventDefault()
      document.execCommand('insertLineBreak')
      onInput()
    } else if (e.key === 'Escape') {
      el.blur()
    }
  }

  function onPaste(e: ClipboardEvent) {
    e.preventDefault()
    const t = e.clipboardData?.getData('text/plain') ?? ''
    document.execCommand('insertText', false, t.replace(/\r\n?/g, '\n'))
    onInput()
  }

  function closeInsert(refocus = false) {
    insertOpen = false
    if (refocus) el?.focus()
  }

  function outside(e: MouseEvent) {
    if (insertOpen && insRoot && !insRoot.contains(e.target as Node)) closeInsert()
  }

  function popKey(e: KeyboardEvent) {
    const items = [...(pop?.querySelectorAll<HTMLButtonElement>('button:not(:disabled)') ?? [])]
    const i = items.indexOf(document.activeElement as HTMLButtonElement)
    if (e.key === 'ArrowDown') items[(i + 1) % items.length]?.focus()
    else if (e.key === 'ArrowUp') items[(i - 1 + items.length) % items.length]?.focus()
    else if (e.key === 'Home') items[0]?.focus()
    else if (e.key === 'End') items[items.length - 1]?.focus()
    else if (e.key === 'Escape') closeInsert(true)
    else if (e.key === 'Tab') closeInsert()
    else return
    if (e.key !== 'Tab') {
      e.preventDefault()
      e.stopPropagation()
    }
  }

  async function openInsert(e?: MouseEvent) {
    insertOpen = !insertOpen
    if (insertOpen) {
      const byKeyboard = e?.detail === 0
      const formats: Record<string, string> = {}
      // {template} is the template's name: not useful inside the text
      for (const t of app.tokens) if (t.name !== 'template') formats[t.name] = `{${t.name}}`
      formats['names:rows'] = '{names:rows}'
      formats['date:yyyy'] = '{date:yyyy}'
      try {
        const r = await post('/api/resolve', { fields: s.meta?.fields, formats })
        insertValues = Object.fromEntries(Object.entries(r).map(([k, v]: any) => [k, v.plain]))
      } catch {
        insertValues = {}
      }
      if (byKeyboard) queueMicrotask(() => pop?.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus())
    }
  }

  function insert(v: string) {
    insertOpen = false
    el.focus()
    // restore caret at end if the editor had no selection
    const sel = window.getSelection()
    if (!sel || !el.contains(sel.anchorNode)) {
      const r = document.createRange()
      r.selectNodeContents(el)
      r.collapse(false)
      sel?.removeAllRanges()
      sel?.addRange(r)
    }
    document.execCommand('insertText', false, v)
    onInput()
  }
</script>

<svelte:window onmousedown={outside} />

<div class="block" class:empty>
  <div class="head row">
    <span class="name">{block.name}</span>
    {#if custom}
      <span class="chip custom" data-tip="Edited by hand: keeps its text when you switch templates. Reset it to follow the template again."><Icon name="pen" size={11} /> Custom</span>
    {:else}
      <span class="chip" data-tip="Filled in from the photo’s details by the template. Type to change it."><Icon name="link" size={11} /> Linked</span>
    {/if}
    <span class="grow"></span>
    <div class="tools">
    <button class="btn ghost sm icon" data-tip="Bold" data-tip-key="Mod+B" aria-label="Bold" onmousedown={(e) => e.preventDefault()} onclick={() => fmt('bold')}><Icon name="bold" size={13} /></button>
    <button class="btn ghost sm icon" data-tip="Italic" data-tip-key="Mod+I" aria-label="Italic" onmousedown={(e) => e.preventDefault()} onclick={() => fmt('italic')}><Icon name="italic" size={13} /></button>
    <div class="ins" bind:this={insRoot}>
      <button class="btn ghost sm icon" data-tip={insertOpen ? undefined : 'Insert a detail from the photo, such as its date or place'} aria-label="Insert field" aria-haspopup="menu" aria-expanded={insertOpen} onmousedown={(e) => e.preventDefault()} onclick={openInsert}><Icon name="insert" size={13} /></button>
      {#if insertOpen}
        <!-- svelte-ignore a11y_interactive_supports_focus -->
        <div class="pop scroll" role="menu" tabindex="-1" aria-label="Insert a field value" bind:this={pop} onkeydown={popKey}>
          {#each Object.entries(insertValues) as [k, v]}
            <button role="menuitem" disabled={!v} onmousedown={(e) => e.preventDefault()} onclick={() => insert(v)}>
              <span class="lab">{label(k)}</span><span class="val">{v || 'empty'}</span>
            </button>
          {/each}
        </div>
      {/if}
    </div>
    <button class="btn ghost sm icon" data-tip={custom ? 'Reset to template: follow the photo’s details again' : 'Already follows the template.'} aria-label="Reset to template" disabled={!custom} onclick={() => app.resetBlock(s, block.id)}><Icon name="reset" size={13} /></button>
    </div>
  </div>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="editor"
    contenteditable="true"
    spellcheck="true"
    role="textbox"
    aria-multiline="true"
    aria-label={block.name}
    tabindex="0"
    bind:this={el}
    oninput={onInput}
    onkeydown={onKey}
    onpaste={onPaste}
    onfocus={() => (focused = true)}
    onblur={() => (focused = false)}
    data-block={block.id}
    onfocusin={() => (app.lastBlock = block.id)}
    data-placeholder={placeholder}
  ></div>
</div>

<style>
  .block { border: 1px solid var(--line); border-radius: 8px; background: var(--bg-2); transition: border-color var(--t-fast) ease, box-shadow var(--t-fast) ease; }
  .block:focus-within { border-color: color-mix(in srgb, var(--accent) 70%, var(--line)); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-link) 16%, transparent); }
  .head { padding: 4px 4px 0 12px; gap: 0; }
  /* the formatting buttons stay quiet until the block is hovered or being edited */
  .tools { display: flex; align-items: center; gap: 0; opacity: 0.55; transition: opacity var(--t-fast) ease; }
  .block:hover .tools, .block:focus-within .tools { opacity: 1; }
  .name { font-weight: 600; font-size: 12px; margin-right: 6px; }
  .chip { display: inline-flex; align-items: center; gap: 3px; font-size: 10.5px; color: var(--fg-3); border: 1px solid var(--line); border-radius: 10px; padding: 0 6px; }
  .chip.custom { color: var(--warn-fg); background: var(--warn-bg); border-color: transparent; }
  .editor { min-height: 44px; padding: 4px 12px 10px; outline: none; white-space: pre-wrap; word-break: break-word; line-height: 1.45; font-size: 13.5px; cursor: text; }
  .editor:empty::before { content: attr(data-placeholder); color: var(--fg-3); font-style: italic; }
  .ins { position: relative; }
  .pop { position: absolute; right: 0; top: 100%; width: 300px; max-height: 280px; background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); z-index: 20; padding: 4px; }
  .pop button { display: flex; flex-direction: column; align-items: flex-start; width: 100%; border: 0; background: none; padding: 5px 8px; border-radius: 5px; text-align: left; cursor: default; }
  .pop button:hover:not(:disabled), .pop button:focus-visible { background: var(--bg-hover); }
  .pop button:disabled { opacity: 0.5; }
  .pop .lab { font-size: 11.5px; font-weight: 600; color: var(--accent); }
  .val { font-size: 12px; color: var(--fg-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; }
</style>
