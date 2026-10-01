<script module lang="ts">
  // Home / Pictures / Desktop quick links: looked up once per app session (only folders that exist)
  let quickCache: { label: string; path: string }[] | null = null
</script>

<script lang="ts">
  import { tick, untrack } from 'svelte'
  import { get } from '../lib/api'
  import { dialogs } from '../lib/dialogs.svelte'
  import Icon from './Icon.svelte'
  import Modal from './Modal.svelte'

  // pick: a one-time id from /api/fs/list; the server grants access only for these
  type Entry = { name: string; path: string; dir: boolean; size?: number; dim?: boolean; pick: string }
  const st = $derived(dialogs.browserState!)
  let dir = $state('')
  let dirPick = $state('') // pick id of the listed folder itself
  let entries = $state<Entry[]>([])
  let parent = $state<string | null>(null)
  let roots = $state<string[]>([])
  let quick = $state<{ label: string; path: string }[]>(quickCache || [])
  let failedPath = $state('') // the path that could not be opened (the list is cleared meanwhile)
  let selected = $state<Set<string>>(new Set())
  let saveName = $state('')
  let error = $state('')
  let pathInput = $state('')
  let pathEdited = false // typed in since the last listing it should reflect
  let active = $state(-1) // keyboard cursor in the list
  let loading = $state(false)
  let listEl = $state<HTMLDivElement>()
  let seq = 0 // only the latest folder request may update the list

  const IMAGES = ['.tif', '.tiff', '.jpg', '.jpeg', '.png']
  const exts: Record<string, string[]> = {
    'open-files': IMAGES,
    'open-font': ['.ttf', '.otf'],
    'open-json': ['.json'],
    'open-folder': [],
    'save-file': IMAGES,
  }
  const isImage = (n: string) => IMAGES.some((x) => n.toLowerCase().endsWith(x))
  const photoCount = $derived(entries.filter((e) => e.dim).length)

  $effect(() => {
    const s = st
    if (s) untrack(() => {
      saveName = s.saveName || ''
      findQuickLinks()
      load(s.initial || '')
    })
  })

  async function findQuickLinks() {
    if (quickCache) return
    const found: { label: string; path: string }[] = []
    try {
      // one listing of the home folder (the default) tells which of these exist (no 404s for missing ones)
      const r = await get('/api/fs/list', { dir: '' })
      found.push({ label: 'Home', path: r.home || r.dir })
      if (!roots.length) roots = r.roots || []
      const dirs = new Map<string, string>((r.entries || []).filter((e: Entry) => e.dir).map((e: Entry) => [e.name.toLowerCase(), e.path]))
      for (const name of ['Pictures', 'Desktop']) {
        const p = dirs.get(name.toLowerCase())
        if (p) found.push({ label: name, path: p })
      }
    } catch {
      /* home not listable: no quick links this time */
      return
    }
    quickCache = found
    quick = found
  }

  async function load(d: string, focusList = false) {
    const my = ++seq
    error = ''
    loading = true
    try {
      const r = await get('/api/fs/list', { dir: d })
      if (my !== seq || !dialogs.browserState) return // a newer request (or close) superseded this one
      failedPath = ''
      dir = r.dir
      dirPick = r.pick
      // show the folder in the path field, unless the user has typed something else meanwhile
      if (!pathEdited || pathInput === d) {
        pathInput = r.dir
        pathEdited = false
      }
      parent = r.parent
      roots = r.roots
      const want = exts[st.kind]
      entries = r.entries
        .filter((e: Entry) => e.dir || want.some((x) => e.name.toLowerCase().endsWith(x)) || (st.kind === 'open-folder' && isImage(e.name)))
        .map((e: Entry) => ({ ...e, dim: !e.dir && st.kind === 'open-folder' }))
      selected = new Set()
      active = -1
      if (focusList) {
        await tick()
        move(0)
      }
    } catch (e: any) {
      if (my === seq) {
        // don't leave the previous folder's listing under the error: it is not what the path says
        failedPath = d
        error = e?.status === 404 ? `There is no folder at “${d}”. Check the path, or pick one of the places above.` : e.message
        entries = []
        selected = new Set()
        active = -1
      }
    } finally {
      if (my === seq) loading = false
    }
  }

  function click(e: MouseEvent | KeyboardEvent, en: Entry, i: number) {
    active = i
    if (en.dim) return
    if (en.dir) {
      if (st.kind === 'open-folder') selected = new Set([en.path])
      return
    }
    if (st.kind === 'save-file') {
      saveName = en.path.split(/[\\/]/).pop() || ''
      return
    }
    const s = new Set(st.kind === 'open-files' && (e.metaKey || e.ctrlKey || e.shiftKey) ? selected : [])
    if (s.has(en.path)) s.delete(en.path)
    else s.add(en.path)
    selected = s
  }

  function dbl(en: Entry) {
    if (en.dir) load(en.path, true)
    else if (!en.dim && st.kind !== 'save-file' && st.kind !== 'open-folder') dialogs.closeBrowser({ picks: [en.pick] })
  }

  function picksFor(ps: Iterable<string>): string[] {
    const byPath = new Map(entries.map((e) => [e.path, e.pick]))
    return [...ps].map((p) => byPath.get(p)).filter((x): x is string => !!x)
  }

  function rows() {
    return [...(listEl?.querySelectorAll<HTMLButtonElement>('.item') ?? [])]
  }
  function move(i: number) {
    const r = rows()
    if (!r.length) return
    active = Math.max(0, Math.min(r.length - 1, i))
    r[active].focus()
    r[active].scrollIntoView({ block: 'nearest' })
  }

  function up() {
    if (parent) load(parent, true)
  }

  function listKey(e: KeyboardEvent) {
    const i = active
    const en = entries[i]
    if (e.key === 'ArrowDown') move(i + 1)
    else if (e.key === 'ArrowUp') move(i < 0 ? 0 : i - 1)
    else if (e.key === 'Home') move(0)
    else if (e.key === 'End') move(entries.length - 1)
    else if (e.key === 'PageDown') move(i + 10)
    else if (e.key === 'PageUp') move(i - 10)
    else if (e.key === 'Backspace' || (e.key === 'ArrowLeft' && e.altKey)) up()
    else if ((e.key === 'Enter' || e.key === ' ') && en) {
      if (en.dir) load(en.path, true)
      else if (en.dim) { /* not selectable here */ }
      else if (e.key === ' ') click(e, en, i)
      else if (st.kind === 'save-file') {
        click(e, en, i)
        done()
      } else {
        if (!selected.has(en.path)) click(e, en, i)
        done()
      }
    } else if (e.key === 'ArrowRight' && en?.dir) load(en.path, true)
    else if (e.key === 'ArrowLeft') up()
    else return
    e.preventDefault()
    e.stopPropagation()
  }

  function done() {
    if (st.kind === 'open-folder') {
      const pick = selected.size ? picksFor([[...selected][0]])[0] : dirPick
      if (pick) dialogs.closeBrowser({ picks: [pick] })
    } else if (st.kind === 'save-file') {
      const name = saveName.trim()
      if (!name) return
      if (/[\\/]/.test(name) || name === '.' || name === '..') {
        error = 'Enter a file name only; choose the folder in the list above.'
        return
      }
      dialogs.closeBrowser({ save: { folder: dirPick, name } })
    } else if (selected.size) dialogs.closeBrowser({ picks: picksFor(selected) })
  }

  const okLabel = $derived(st?.kind === 'open-folder' ? 'Choose folder' : st?.kind === 'save-file' ? 'Save' : 'Open')
  // nothing can be chosen while the typed path is not a folder
  const canOk = $derived(!failedPath && !loading && (st?.kind === 'open-folder' ? !!dirPick : st?.kind === 'save-file' ? !!saveName.trim() && !!dirPick : selected.size > 0))
  const tabStop = $derived(active >= 0 ? active : 0)
  const okTip = $derived(
    loading ? 'Loading the folder…'
      : failedPath ? 'This folder couldn’t be opened. Fix the path or pick a place above.'
      : st?.kind === 'open-folder' ? (selected.size ? 'Use the highlighted folder.' : `Use the folder you are in${dir ? ` (${dir.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || dir})` : ''}.`)
      : st?.kind === 'save-file' ? (saveName.trim() ? 'Save with this name in the folder you are in.' : 'Type a file name first.')
      : selected.size ? `Open the ${selected.size === 1 ? 'chosen file' : `${selected.size} chosen files`}.` : 'Click a file to choose it first.',
  )
</script>

{#if st}
  <Modal title={st.title || (st.kind === 'open-folder' ? 'Choose a folder' : 'Choose files')} width={680} height="min(620px, calc(100vh - 48px))" noPad onclose={() => dialogs.closeBrowser(null)}>
    <div class="fb">
      <div class="bar row">
        <button class="btn sm icon" aria-label="Up one folder" data-tip={parent ? 'Go up to the folder that contains this one.' : 'This is the top folder; there is nothing above it.'} data-tip-key="Backspace" disabled={!parent} onclick={up}><Icon name="chevleft" /></button>
        <input class="field grow" bind:value={pathInput} oninput={() => (pathEdited = true)} onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); load(pathInput) } else if (e.key === 'ArrowDown') { e.preventDefault(); move(0) } }} aria-label="Folder path" />
      </div>
      <div class="places row" role="group" aria-label="Places">
        {#each quick as q}<button class="btn sm ghost" data-tip={q.path} onclick={() => load(q.path, true)}><Icon name={q.label === 'Pictures' ? 'image' : 'folder'} size={14} /> {q.label}</button>{/each}
        {#each roots.filter((r) => !quick.some((q) => q.path === r)) as r}<button class="btn sm ghost" data-tip={r === '/' ? 'The whole computer (/)' : r} onclick={() => load(r, true)}>{r === '/' ? 'Computer' : r}</button>{/each}
      </div>
      {#if error}
        <div class="err row" role="alert">
          <span class="grow">{error}</span>
          {#if failedPath && dir}<button class="btn sm" data-tip="Go back to the last folder that opened." onclick={() => load(dir, true)}>Back to {dir.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || dir}</button>{/if}
        </div>
      {/if}
      <!-- svelte-ignore a11y_interactive_supports_focus -->
      <div class="list scroll" role="listbox" aria-label="Files and folders" aria-multiselectable={st.kind === 'open-files'} bind:this={listEl} onkeydown={listKey}>
        {#each entries as en, i (en.path)}
          <button
            class="item" class:sel={selected.has(en.path)} class:act={i === active} class:dim={en.dim}
            role="option" aria-selected={selected.has(en.path)} aria-disabled={en.dim || undefined}
            tabindex={i === tabStop ? 0 : -1}
            data-tip={en.dim ? 'Shown so you can see what’s inside. Choose the folder that holds the photos.' : en.name.length > 48 ? en.name : undefined}
            onclick={(e) => click(e, en, i)} ondblclick={() => dbl(en)} onfocus={() => (active = i)}
          >
            <Icon name={en.dir ? 'folder' : en.dim ? 'image' : 'file'} />
            <span class="grow name">{en.name}</span>
            {#if !en.dir && en.size}<span class="faint">{(en.size / 1048576).toFixed(1)} MB</span>{/if}
          </button>
        {:else}
          <div class="empty faint">{loading ? 'Loading…' : failedPath ? 'Nothing to show.' : 'Nothing to show in this folder.'}</div>
        {/each}
      </div>
      {#if st.kind === 'open-folder' && photoCount}
        <div class="count small">{photoCount} photo{photoCount === 1 ? '' : 's'} in this folder</div>
      {/if}
      {#if st.kind === 'save-file'}
        <div class="row bar"><span class="muted">File name</span><input class="field grow" bind:value={saveName} onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); done() } }} aria-label="File name" /></div>
      {/if}
    </div>
    {#snippet footer()}
      <span class="grow faint hint">{st.kind === 'open-files' ? 'Double-click a folder to open it. Hold Ctrl or ⌘ to pick several files.' : st.kind === 'open-folder' ? 'Double-click a folder to look inside it.' : 'Double-click a folder to open it.'}</span>
      <button class="btn" onclick={() => dialogs.closeBrowser(null)}>Cancel</button>
      <button class="btn primary" disabled={!canOk} data-tip={okTip} onclick={done}>{okLabel}</button>
    {/snippet}
  </Modal>
{/if}

<style>
  .fb { display: flex; flex-direction: column; height: 100%; }
  .bar { padding: 10px 14px; border-bottom: 1px solid var(--line); }
  .bar:last-child { border-top: 1px solid var(--line); border-bottom: 0; }
  .places { padding: 6px 14px; gap: 4px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .list { flex: 1; min-height: 0; padding: 6px; }
  .item { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: none; padding: 5px 8px; border-radius: 5px; text-align: left; cursor: default; }
  .item:hover { background: var(--bg-hover); }
  .item:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
  .item.sel { background: color-mix(in srgb, var(--accent) 22%, transparent); }
  .item.dim { color: var(--fg-3); }
  .item.dim:hover { background: none; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .empty { padding: 24px; text-align: center; }
  .count { padding: 6px 14px; border-top: 1px solid var(--line); color: var(--fg-2); }
  .small { font-size: 12px; }
  .err { padding: 8px 14px; color: var(--err-fg); background: var(--err-bg); }
  .hint { font-size: 12px; }
</style>
