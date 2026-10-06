<script lang="ts">
  // The photo's date as Year / Month / Day boxes, each optional (blank: unknown), and one
  // "Estimated" box (the finest part filled in is a best guess: around Thanksgiving, the summer of
  // 1944, the 1920s; printed "c."). Saved as photokin does: a filled-in DateTimeOriginal at
  // midnight plus a "DATE: Y!M~" keyword saying which parts are known, guessed or unknown, and read
  // back the same way. Advanced shows (and takes) the stored date and that keyword as they are.
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { post } from '../lib/api'
  import { advancedOf, dateBoxes, dateState, editFromBoxes, guessedPart, normDate, parseAdvanced, type NormDate } from '../lib/metaedits'

  let { s }: { s: PhotoSession } = $props()

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

  const edited = $derived(s.draft.meta ? 'date' in s.draft.meta : false)
  const fileState = $derived(dateState(s.meta?.fields))
  const fileDate = $derived(fileState.kind === 'date' ? normDate(fileState) : null)
  const shown = $derived.by(() => {
    if (!edited) return fileState
    const d = s.draft.meta!.date
    return d ? ({ kind: 'date', ...normDate(d) } as const) : ({ kind: 'none' } as const)
  })
  const shownDate = $derived<NormDate | null>(shown.kind === 'date' ? normDate(shown) : null)

  // the inputs: filled from the date shown, kept while a date is still being typed
  let year = $state<number | null>(null)
  let month = $state<number | null>(null)
  let day = $state<number | null>(null)
  let estimate = $state(false)
  let advStored = $state('')
  let advKeyword = $state('')
  // the inputs as the date was shown: a problem only counts while they differ from it (an edit
  // under way); going back to what was shown is never one
  let shownBoxes = $state('')
  let shownAdv = $state('')
  const boxes = $derived(JSON.stringify([year, month, day, estimate]))
  const adv = $derived(JSON.stringify([advStored, advKeyword]))
  let lastKey = ''
  $effect(() => {
    const key = JSON.stringify([s.path, shown])
    if (key === lastKey) return
    lastKey = key
    if (shown.kind === 'date') {
      const d = normDate(shown)
      ;({ year, month, day } = dateBoxes(d))
      estimate = d.estimate
      ;({ stored: advStored, keyword: advKeyword } = advancedOf(d, 'stored' in shown ? shown.stored : undefined))
    } else {
      // none, or a date in words ("1950s"): what is known of it is filled in
      year = shown.kind === 'text' ? shown.year : null
      month = day = null
      estimate = false
      advStored = advKeyword = ''
    }
    shownBoxes = JSON.stringify([year, month, day, estimate])
    shownAdv = JSON.stringify([advStored, advKeyword])
  })

  // the date the boxes keep a pattern of: the one shown, or the file's own when Estimated is back
  // to what the file says (a guessed year with a known birthday comes back after a toggle)
  const keep = $derived([shownDate, fileDate].find((k) => k?.pattern && k.estimate === estimate) ?? shownDate ?? fileDate)
  const fromBoxes = $derived(editFromBoxes(year, month, day, estimate, keep))
  const fromAdv = $derived(parseAdvanced(advStored, advKeyword))
  const problem = $derived(
    adv !== shownAdv && 'problem' in fromAdv ? fromAdv.problem :
    boxes !== shownBoxes && 'problem' in fromBoxes ? fromBoxes.problem : '')
  const badAdv = $derived(adv !== shownAdv && 'problem' in fromAdv ? fromAdv.field : '')
  // a date that isn't one yet is not what a save would write: saving waits until it is (or cleared)
  $effect(() => {
    const session = s
    session.invalidDetail = problem ? `Finish the date in the Metadata tab first (or clear it): ${problem}` : null
    return () => { session.invalidDetail = null }
  })

  function applyBoxes() {
    if ('edit' in fromBoxes && boxes !== shownBoxes) app.setDate(s, fromBoxes.edit)
  }
  function applyAdv() {
    if ('edit' in fromAdv && adv !== shownAdv) app.setDate(s, fromAdv.edit)
  }
  const num = (v: string) => (v.trim() === '' ? null : +v)

  const guessNote = $derived(estimate && 'edit' in fromBoxes && fromBoxes.edit ? guessedPart(fromBoxes.edit) : '')
  // what Advanced says about parts that are stored but not shown
  const advNote = $derived.by(() => {
    if (!shownDate) return ''
    const b = dateBoxes(shownDate)
    const st = 'stored' in shown && shown.stored ? shown.stored : shownDate.iso
    if (b.year === null) return `The year ${st.slice(0, 4)} is only a placeholder (EXIF needs one); the keyword says it is unknown, so it is never printed.`
    if (st.length > shownDate.iso.length) return 'The file stores more of the date than is known; the keyword says which parts count, so the rest is never printed.'
    if (shownDate.level !== 'day') return `Saved to EXIF with a placeholder ${shownDate.level === 'year' ? 'June 15' : '15th'} (EXIF needs a whole date); it is never printed.`
    return ''
  })

  // what the caption prints for the date as it will be saved
  let prints = $state('')
  $effect(() => {
    const body = { ...app.fieldsBody(s), formats: { d: '{date}' } }
    void s.draft.meta
    let live = true
    const t = setTimeout(() => {
      post<Record<string, { plain?: string }>>('/api/resolve', body).then((r) => { if (live) prints = r.d?.plain ?? '' }).catch(() => {})
    }, 120)
    return () => { live = false; clearTimeout(t) }
  })
  const daysIn = $derived(month ? new Date(year ?? 2000, month, 0).getDate() : 31)
</script>

<div class="datef col">
  <p class="small faint hint">All optional. Fill in what you know or guess.</p>
  {#if shown.kind === 'text' && !edited}
    <p class="small astext" data-tip="The file gives the date in words, so it is printed as written. Set a date to replace it.">“{shown.text}”</p>
  {/if}
  <div class="parts">
    <label class="part"><span>Year</span>
      <input class="field" type="number" min="1000" max={new Date().getFullYear() + 1} step="1" placeholder="unknown" aria-label="Year" value={year ?? ''}
        oninput={(e) => { year = num((e.target as HTMLInputElement).value); applyBoxes() }} /></label>
    <label class="part"><span>Month</span>
      <select class="field" class:blank={month === null} aria-label="Month" value={month ?? ''}
        onchange={(e) => { month = num((e.target as HTMLSelectElement).value); applyBoxes() }}>
        <option value="">unknown</option>
        {#each MONTHS as m, i}<option value={i + 1}>{m}</option>{/each}
      </select></label>
    <label class="part"><span>Day</span>
      <input class="field" type="number" min="1" max={daysIn} step="1" placeholder="unknown" aria-label="Day" value={day ?? ''}
        oninput={(e) => { day = num((e.target as HTMLInputElement).value); applyBoxes() }} /></label>
  </div>
  <div class="row est-row">
    <label class="row est" data-tip="The last part filled in is a best guess: around Thanksgiving (the 23rd), the summer of 1944 (July), the 1920s (1925). Printed with “c.”.">
      <input type="checkbox" checked={estimate} onchange={(e) => { estimate = (e.target as HTMLInputElement).checked; applyBoxes() }} /> Estimated{#if guessNote}<span class="faint">&nbsp;({guessNote})</span>{/if}
    </label>
    {#if prints && !problem}
      <span class="prints"><span class="faint">Prints as</span> <span class="pv">{prints}</span></span>
    {/if}
  </div>
  {#if problem}
    <p class="small warnline">{problem}{prints ? ` Until then the date stays ${prints}.` : ''}</p>
  {/if}
  <details class="adv">
    <summary><span class="car" aria-hidden="true"></span>Advanced</summary>
    <div class="advb col">
      <div class="row gap">
        <span class="faint small">Stored</span>
        <input class="field mono st" class:bad={badAdv === 'stored'} aria-label="Stored date" placeholder="YYYY-MM-DD" spellcheck="false" value={advStored}
          data-tip="The date as stored: YYYY, YYYY-MM or YYYY-MM-DD"
          oninput={(e) => { advStored = (e.target as HTMLInputElement).value; applyAdv() }} />
        <input class="field mono kw" class:bad={badAdv === 'keyword'} aria-label="Date keyword" placeholder="DATE: Y!M!D!" spellcheck="false" value={advKeyword}
          data-tip="photokin’s date keyword: Y, M and D, each with ! known, ~ a best guess or ? unknown (DATE: Y~M!D! is a known birthday in a guessed year)"
          oninput={(e) => { advKeyword = (e.target as HTMLInputElement).value; applyAdv() }} />
      </div>
      {#if advNote}<p class="small faint">{advNote}</p>{/if}
    </div>
  </details>
</div>

<style>
  .datef { gap: 6px; min-width: 0; }
  .gap { gap: 6px; }
  .hint { margin-top: -2px; }
  .parts { display: grid; grid-template-columns: 84px minmax(0, 1fr) 84px; gap: 6px; }
  .part { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .part > span { font-size: 11px; color: var(--fg-3); }
  .part .field { width: 100%; }
  .part input::placeholder { font-style: italic; }
  select.blank { color: var(--fg-3); font-style: italic; }
  select option { color: var(--fg); font-style: normal; }
  .est-row { gap: 10px; flex-wrap: wrap; align-items: center; justify-content: space-between; margin-top: 2px; }
  .est { gap: 5px; font-size: 12px; color: var(--fg-2); cursor: pointer; align-items: center; }
  .prints { font-size: 12px; }
  .small { font-size: 12px; margin: 0; }
  .astext { color: var(--fg); font-style: italic; }
  .warnline { color: var(--warn-fg); }
  .pv { color: var(--fg); }
  .adv { margin-top: 4px; border-top: 1px solid var(--line); padding-top: 6px; }
  .adv summary { list-style: none; cursor: pointer; font-size: 12px; color: var(--fg-2); display: flex; align-items: center; gap: 6px; user-select: none; width: max-content; }
  .adv summary::-webkit-details-marker { display: none; }
  .car { width: 0; height: 0; border-style: solid; border-width: 5px 0 5px 7px; border-color: transparent transparent transparent currentColor; transition: transform 0.12s; }
  .adv[open] .car { transform: rotate(90deg); }
  .advb { gap: 6px; margin-top: 8px; }
  .mono { font-family: var(--font-mono, ui-monospace, Consolas, monospace); font-size: 12px; }
  .st { width: 112px; flex: none; }
  .kw { flex: 1; min-width: 0; }
  .bad { border-color: var(--danger, #c0392b); box-shadow: 0 0 0 2px color-mix(in srgb, var(--danger, #c0392b) 20%, transparent); }
</style>
