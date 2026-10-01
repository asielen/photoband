<script lang="ts">
  // A caption format field: raw format text (for power users) with plain-language help on top:
  // typing "{" lists fields by name with an example ("Date — June 14, 1952"), "Insert field"
  // does the same from a button, and a live example of the result sits under the field.
  import { post } from '../lib/api'
  import { app } from '../lib/store.svelte'
  import { fieldChoices, fieldLabel, filterChoices, type FieldChoice } from '../lib/tokenlabels'
  import type { Issue } from '../lib/types'
  import Icon from './Icon.svelte'

  let {
    value,
    onchange,
    rows = 2,
    label = '',
    extraTokens = [] as string[],
    single = false,
    example = undefined,
  }: {
    value: string
    onchange: (v: string) => void
    rows?: number
    label?: string
    extraTokens?: string[]
    single?: boolean
    /** live result for the current photo: undefined hides the line, null shows "—" */
    example?: { plain?: string; empty?: boolean; empty_tokens?: string[] } | null
  } = $props()

  let ta: HTMLTextAreaElement
  let issues = $state<Issue[]>([])
  // complete: replaces the "{partial" being typed; insert: puts a field at the saved selection
  let menu = $state<{ mode: 'complete' | 'insert'; items: FieldChoice[]; index: number; start: number; end: number } | null>(null)
  let menuEl = $state<HTMLDivElement>()
  let timer: any
  const uid = Math.random().toString(36).slice(2, 8)

  // Replies to /api/validate can arrive out of order; only the latest request counts.
  let seq = 0
  $effect(() => {
    const v = value
    const extra = extraTokens
    const my = ++seq
    clearTimeout(timer)
    timer = setTimeout(async () => {
      let got: Issue[] = []
      try {
        got = await post<Issue[]>('/api/validate', { format: v })
        if (extra.length) got = got.filter((i) => !extra.some((t) => v.slice(i.start, i.end).startsWith('{' + t)))
      } catch {
        got = []
      }
      if (my === seq) issues = got
    }, 150)
  })
  // while a field is being typed ("{da"), "unclosed {" is expected: don't shout about it yet
  const shownIssues = $derived(menu?.mode === 'complete' ? issues.filter((i) => i.start !== menu!.start) : issues)

  /** Token-looking spans: an unescaped { up to the next } on the same line. */
  function tokenSpans(v: string): [number, number][] {
    const out: [number, number][] = []
    let i = 0
    while (i < v.length) {
      const ch = v[i]
      if (ch === '\\') {
        i += 2
        continue
      }
      if (ch === '{') {
        let j = i + 1
        while (j < v.length && v[j] !== '}' && v[j] !== '{' && v[j] !== '\n') j += v[j] === '\\' ? 2 : 1
        if (j < v.length && v[j] === '}') {
          out.push([i, j + 1])
          i = j + 1
          continue
        }
      }
      i++
    }
    return out
  }

  // The overlay is built from the raw text in segments (never by regex over generated HTML),
  // so it stays aligned with the textarea. It is aria-hidden and takes no pointer events: the
  // issue messages are listed under the field instead.
  const html = $derived.by(() => {
    const v = value
    const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    const marks: { start: number; end: number }[] = []
    let last = 0
    for (const s of [...shownIssues].sort((a, b) => a.start - b.start)) {
      const start = Math.max(0, Math.min(s.start, v.length))
      const end = Math.min(v.length, Math.max(s.end, s.start + 1))
      if (start < last || end <= start) continue
      marks.push({ start, end })
      last = end
    }
    const toks = tokenSpans(v)
    const cuts = new Set<number>([0, v.length])
    for (const m of marks) cuts.add(m.start).add(m.end)
    for (const [a, b] of toks) cuts.add(a).add(b)
    const pts = [...cuts].sort((a, b) => a - b)
    let out = ''
    for (let k = 0; k + 1 < pts.length; k++) {
      const a = pts[k]
      const b = pts[k + 1]
      if (b <= a) continue
      let h = esc(v.slice(a, b))
      if (toks.some(([ta, tb]) => ta <= a && b <= tb)) h = `<span class="tok">${h}</span>`
      if (marks.some((x) => x.start <= a && b <= x.end)) h = `<mark>${h}</mark>`
      out += h
    }
    return out + '\n'
  })

  const choices = $derived(fieldChoices(app.tokens || [], extraTokens))

  function update(e: Event) {
    const v = (e.target as HTMLTextAreaElement).value
    onchange(single ? v.replace(/\n/g, '') : v)
    const pos = ta.selectionStart
    const before = v.slice(0, pos)
    const open = before.lastIndexOf('{')
    // escaped only when an odd number of backslashes precede it: "\\{" starts a real token
    let bs = 0
    while (open - 1 - bs >= 0 && before[open - 1 - bs] === '\\') bs++
    if (open >= 0 && bs % 2 === 0 && !before.slice(open).includes('}') && !/[|:\s]/.test(before.slice(open + 1))) {
      const items = filterChoices(choices, before.slice(open + 1))
      menu = items.length ? { mode: 'complete', items, index: 0, start: open, end: pos } : null
    } else if (menu?.mode === 'complete') menu = null
  }

  function choose(c: FieldChoice) {
    if (!menu) return
    const tokenText = '{' + c.insert + '}'
    let v: string
    let caret: number
    if (menu.mode === 'complete') {
      const pos = ta.selectionStart
      let after = value.slice(pos)
      if (after.startsWith('}')) after = after.slice(1)
      v = value.slice(0, menu.start) + tokenText + after
      caret = menu.start + tokenText.length
    } else {
      v = value.slice(0, menu.start) + tokenText + value.slice(menu.end)
      caret = menu.start + tokenText.length
    }
    onchange(v)
    menu = null
    queueMicrotask(() => {
      ta.focus()
      ta.setSelectionRange(caret, caret)
    })
  }

  function openInsert() {
    if (menu?.mode === 'insert') {
      menu = null
      return
    }
    // before the field was ever clicked, add to the end
    const start = ta && caretKnown ? ta.selectionStart : value.length
    const end = ta && caretKnown ? ta.selectionEnd : start
    menu = { mode: 'insert', items: choices, index: 0, start, end }
    queueMicrotask(() => menuEl?.querySelector<HTMLButtonElement>('button[role=option]')?.focus())
  }

  function closeMenu(refocus: boolean) {
    menu = null
    if (refocus) queueMicrotask(() => ta?.focus())
  }

  function move(d: number) {
    if (!menu) return
    menu.index = (menu.index + d + menu.items.length) % menu.items.length
    if (menu.mode === 'insert') menuEl?.querySelectorAll<HTMLButtonElement>('button[role=option]')[menu.index]?.focus()
    else menuEl?.querySelectorAll<HTMLElement>('[role=option]')[menu.index]?.scrollIntoView({ block: 'nearest' })
  }

  function key(e: KeyboardEvent) {
    if (!menu || menu.mode !== 'complete') {
      if (single && e.key === 'Enter') e.preventDefault()
      return
    }
    if (e.key === 'ArrowDown') {
      move(1)
      e.preventDefault()
    } else if (e.key === 'ArrowUp') {
      move(-1)
      e.preventDefault()
    } else if (e.key === 'Enter' || e.key === 'Tab') {
      choose(menu.items[menu.index])
      e.preventDefault()
    } else if (e.key === 'Escape') {
      menu = null
      e.preventDefault()
      e.stopPropagation()
    }
  }

  function menuKey(e: KeyboardEvent) {
    if (!menu || menu.mode !== 'insert') return
    if (e.key === 'ArrowDown') move(1)
    else if (e.key === 'ArrowUp') move(-1)
    else if (e.key === 'Home') move(-menu.index)
    else if (e.key === 'End') move(menu.items.length - 1 - menu.index)
    else if (e.key === 'Escape') closeMenu(true)
    else if (e.key === 'Tab') {
      menu = null
      return
    } else return
    e.preventDefault()
    e.stopPropagation()
  }

  function menuFocusOut(e: FocusEvent) {
    if (menu?.mode === 'insert' && !(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) menu = null
  }

  const missing = $derived((example?.empty_tokens || []).map(fieldLabel))
  let back: HTMLDivElement
  let caretKnown = false
</script>

<div class="ff">
  <div class="wrap">
    <div class="back" bind:this={back} aria-hidden="true">{@html html}</div>
    <textarea
      bind:this={ta} class="field" {rows} value={value} oninput={update} onkeydown={key} onfocus={() => (caretKnown = true)}
      onblur={() => setTimeout(() => { if (menu?.mode === 'complete') menu = null }, 150)}
      onscroll={() => back && (back.scrollTop = ta.scrollTop)}
      spellcheck="false" aria-label={label} aria-invalid={shownIssues.length > 0}
      aria-autocomplete="list" aria-controls={menu ? `ffm-${uid}` : undefined}
      aria-activedescendant={menu?.mode === 'complete' ? `ffm-${uid}-${menu.index}` : undefined}
    ></textarea>
    {#if menu}
      <!-- svelte-ignore a11y_interactive_supports_focus -->
      <div class="menu scroll" role="listbox" id="ffm-{uid}" aria-label="Fields" bind:this={menuEl} onkeydown={menuKey} onfocusout={menuFocusOut}>
        {#if menu.mode === 'insert'}<div class="mhead faint">Insert a field from the photo’s information</div>{/if}
        {#each menu.items as it, i}
          <button
            type="button" role="option" id="ffm-{uid}-{i}" aria-selected={i === menu.index} class:on={i === menu.index}
            tabindex={menu.mode === 'insert' && i === menu.index ? 0 : -1}
            data-tip-off
            onmousedown={(e) => { if (menu?.mode === 'complete') e.preventDefault() }}
            onmouseenter={() => menu && (menu.index = i)}
            onclick={() => choose(it)}
          >
            <span class="lab">{it.label}</span>{#if it.example}<span class="ex">— {it.example}</span>{/if}
            <code>{'{'}{it.insert}{'}'}</code>
          </button>
        {/each}
      </div>
    {/if}
  </div>
  <div class="under">
    {#if example !== undefined}
      <div class="example" aria-live="polite">
        <span class="faint">Example:</span>
        {#if example === null}<span class="faint">—</span>
        {:else if example.empty}<i class="faint">nothing for this photo (this part takes no space)</i>
        {:else}<span class="pre">{example.plain}</span>{/if}
        {#if missing.length}<span class="faint miss" data-tip="These fields have no value in the photo’s information, so they are left out.">· not in this photo: {missing.join(', ')}</span>{/if}
      </div>
    {:else}<span class="grow"></span>{/if}
    <button type="button" class="btn sm ghost ins" aria-haspopup="listbox" aria-expanded={menu?.mode === 'insert'}
      data-tip="Add a field such as the date or the people’s names at the cursor. You can also type {'{'} in the field."
      onmousedown={(e) => e.preventDefault()} onclick={openInsert}><Icon name="insert" size={13} /> Insert field</button>
  </div>
  {#if shownIssues.length}
    <div class="issues" role="status">{shownIssues.map((i) => i.message).join(' · ')}</div>
  {/if}
</div>

<style>
  .ff { position: relative; }
  .wrap { position: relative; }
  .back, textarea { font: 12.5px/1.5 var(--font-mono); padding: 6px 8px; white-space: pre-wrap; word-wrap: break-word; overflow-wrap: break-word; letter-spacing: 0; }
  .back { position: absolute; inset: 0; color: transparent; border: 1px solid transparent; overflow: hidden; pointer-events: none; border-radius: var(--radius-sm); }
  .back :global(mark) { background: transparent; color: transparent; text-decoration: underline wavy var(--danger); text-underline-offset: 3px; }
  .back :global(.tok) { background: color-mix(in srgb, var(--accent) 14%, transparent); border-radius: 3px; }
  textarea { position: relative; width: 100%; background: transparent; resize: vertical; min-height: 34px; display: block; }
  .field { background: transparent; }
  .wrap::before { content: ''; position: absolute; inset: 0; background: var(--bg-2); border-radius: var(--radius-sm); }
  .menu { position: absolute; left: 0; right: 0; top: calc(100% + 2px); max-height: 260px; background: var(--bg-2); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); z-index: 30; padding: 4px; }
  .mhead { font-size: 11.5px; padding: 4px 8px 6px; }
  .menu button { display: flex; gap: 6px; align-items: baseline; width: 100%; border: 0; background: none; text-align: left; padding: 5px 8px; min-height: 28px; border-radius: 5px; cursor: default; color: var(--fg); }
  .menu button:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent-link); }
  .menu button.on { background: var(--accent); color: var(--accent-fg); }
  .lab { font-weight: 600; font-size: 12.5px; flex: none; }
  .ex { font-size: 12.5px; opacity: 0.85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
  .menu code { font: 11px var(--font-mono); opacity: 0.6; margin-left: auto; flex: none; padding-left: 8px; }
  .under { display: flex; align-items: flex-start; gap: 8px; margin-top: 4px; min-height: 24px; }
  .example { flex: 1; min-width: 0; font-size: 12.5px; padding-top: 3px; }
  .pre { white-space: pre-wrap; }
  .miss { margin-left: 2px; }
  .ins { flex: none; color: var(--fg-2); }
  .issues { color: var(--danger); font-size: 11.5px; margin-top: 2px; }
</style>
