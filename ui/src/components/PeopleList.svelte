<script lang="ts">
  // The people in the photo, grouped in rows as {names:rows} prints them, front row first, each left
  // to right. Names are edited in place; hovering a name outlines its face on the photo.
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { post } from '../lib/api'
  import { leftToRight } from '../lib/metaedits'
  import type { Face } from '../lib/types'
  import Segmented from './Segmented.svelte'

  let { s }: { s: PhotoSession } = $props()

  const faces = $derived(app.faces(s))
  const positioned = $derived(leftToRight(faces.named))
  const unplaced = $derived(faces.named.filter((f) => !f.box))
  const rowsUsed = $derived(!!app.effective(s)?.blocks.some((b) => b.format.includes('{names:rows')))

  // the row grouping comes from the server (the same code as the caption), as indexes into fields.faces
  let groups = $state<number[][]>([])
  $effect(() => {
    const body = { ...app.fieldsBody(s), formats: {}, rows: true }
    void s.draft.meta
    void s.draft.faceRows
    let live = true
    const t = setTimeout(() => {
      post<{ _rows?: number[][] }>('/api/resolve', body).then((r) => { if (live) groups = r._rows ?? [] }).catch(() => {})
    }, 80)
    return () => { live = false; clearTimeout(t) }
  })
  // the server answers with indexes into the effective named faces (as it applies the edits)
  const rows = $derived.by((): Face[][] => {
    const named = faces.named
    const out = groups.map((g) => g.map((i) => named[i]).filter(Boolean))
    return out.length && out.flat().length === positioned.length ? out : [positioned]
  })
  const rowLabel = (i: number, n: number) => (n < 2 ? '' : i === 0 ? 'Front row' : i === n - 1 ? 'Back row' : `Row ${i + 1}`)
  const ROWS = [{ value: 'auto', label: 'Auto', tip: 'Group people into rows by how high their faces are' }, ...[1, 2, 3, 4].map((n) => ({ value: String(n), label: String(n), tip: `Always ${n} row${n > 1 ? 's' : ''} for this photo` }))]

  function rename(f: Face, v: string) {
    if (f.key && v.trim() !== f.name) app.updateFace(s, f.key, { name: v })
  }
  function select(f: Face) {
    if (!f.key) return
    app.showFaces = true
    app.selectedFace = f.key
  }
  function place(f: Face) {
    if (!f.key) return
    app.showFaces = true
    app.faceToolTarget = f.key
    app.faceTool = 'add'
  }
  function nameKey(e: KeyboardEvent, f: Face) {
    e.stopPropagation()
    if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
    else if (e.key === 'Escape') {
      ;(e.target as HTMLInputElement).value = f.name
      ;(e.target as HTMLInputElement).blur()
    }
  }
  const number = (f: Face) => positioned.indexOf(f) + 1
</script>

<section class="col people">
  <div class="row head">
    <h3 class="section-h">People, left to right</h3>
    <span class="grow"></span>
    {#if positioned.length > 1}
      <Segmented small label="Rows in the caption" value={s.draft.faceRows ? String(s.draft.faceRows) : 'auto'} options={ROWS}
        onchange={(v) => app.setFaceRows(s, v === 'auto' ? null : +v)} />
    {/if}
  </div>
  {#if positioned.length > 1 && !rowsUsed}
    <p class="faint small">Rows apply where a caption line uses “People, by row”.</p>
  {/if}
  {#if !faces.named.length && !faces.unnamed.length}
    <p class="faint small">No faces are tagged. Turn on <b>Faces</b> below the photo and use <b>Add face</b> to mark one.</p>
  {/if}
  {#each rows as row, ri}
    {#if rows.length > 1}<div class="rowname">{rowLabel(ri, rows.length)}</div>{/if}
    <ol class="list">
      {#each row as f (f.key)}
        <li class="person" class:hover={app.hoverFace === f.key} class:sel={app.selectedFace === f.key}
          onpointerenter={() => (app.hoverFace = f.key ?? null)} onpointerleave={() => (app.hoverFace = null)}>
          <button class="num" aria-label={`Show ${f.name} on the photo`} data-tip="Show on the photo" onclick={() => select(f)}>{number(f)}</button>
          <input class="nm" value={f.name} aria-label={`Name of person ${number(f)}`} onkeydown={(e) => nameKey(e, f)}
            onchange={(e) => rename(f, (e.target as HTMLInputElement).value)} onfocus={() => (app.hoverFace = f.key ?? null)} />
          <button class="btn sm ghost icon del" aria-label={`Remove ${f.name}`} data-tip="Remove this face tag" onclick={() => f.key && app.deleteFace(s, f.key)}>×</button>
        </li>
      {/each}
    </ol>
  {/each}
  {#if unplaced.length}
    <div class="rowname">Not marked on the photo</div>
    <ul class="list">
      {#each unplaced as f (f.key)}
        <li class="person">
          <span class="num off" aria-hidden="true">?</span>
          <input class="nm" value={f.name} aria-label="Name" onkeydown={(e) => nameKey(e, f)} onchange={(e) => rename(f, (e.target as HTMLInputElement).value)} />
          <button class="btn sm ghost mark" data-tip="Draw a box around this person’s face, so the name has its place in the left-to-right order" onclick={() => place(f)}>Mark</button>
          <button class="btn sm ghost icon del" aria-label={`Remove ${f.name}`} data-tip="Remove this name" onclick={() => f.key && app.deleteFace(s, f.key)}>×</button>
        </li>
      {/each}
    </ul>
  {/if}
  {#if faces.unnamed.length}
    <div class="rowname">Without a name</div>
    <ul class="list">
      {#each faces.unnamed as f (f.key)}
        <li class="person" class:hover={app.hoverFace === f.key} class:sel={app.selectedFace === f.key}
          onpointerenter={() => (app.hoverFace = f.key ?? null)} onpointerleave={() => (app.hoverFace = null)}>
          <button class="num off" aria-label="Show this face on the photo" data-tip="Show on the photo" onclick={() => select(f)}>·</button>
          <input class="nm" value="" placeholder="Who is this?" aria-label="Name this face" onkeydown={(e) => nameKey(e, f)}
            onchange={(e) => rename(f, (e.target as HTMLInputElement).value)} onfocus={() => (app.hoverFace = f.key ?? null)} />
          <button class="btn sm ghost icon del" aria-label="Remove this face" data-tip="Remove this face box" onclick={() => f.key && app.deleteFace(s, f.key)}>×</button>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .people { gap: 6px; }
  .head { gap: 8px; align-items: center; min-height: 28px; }
  .grow { flex: 1; }
  .small { font-size: 12px; margin: 0; }
  .rowname { font-size: 11px; font-weight: 600; letter-spacing: 0.03em; text-transform: uppercase; color: var(--fg-3); margin-top: 4px; }
  .list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .person { display: flex; align-items: center; gap: 6px; padding: 2px 2px 2px 0; border-radius: var(--radius); }
  .person.hover, .person.sel { background: var(--bg-hover); }
  .num { flex: none; width: 22px; height: 22px; border-radius: 50%; border: 0; background: #ffd23f; color: #1d1d1f; font-size: 11px; font-weight: 700; cursor: pointer; padding: 0; }
  .num.off { background: transparent; border: 1px dashed var(--line-2); color: var(--fg-3); cursor: default; display: inline-grid; place-items: center; }
  button.num.off { cursor: pointer; }
  .nm { flex: 1; min-width: 0; height: 26px; border: 1px solid transparent; border-radius: var(--radius); background: transparent; font: inherit; font-size: 12.5px; color: var(--fg); padding: 0 6px; }
  .nm:hover { border-color: var(--line-2); }
  .nm:focus { border-color: var(--accent-link); box-shadow: var(--focus); outline: none; background: var(--bg-2); }
  .del { opacity: 0; transition: opacity var(--t-fast) ease; }
  .person:hover .del, .person:focus-within .del { opacity: 1; }
  .mark { flex: none; }
</style>
