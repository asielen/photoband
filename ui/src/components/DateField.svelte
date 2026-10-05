<script lang="ts">
  // The photo's date with its level of detail (a day, a month or a year) and whether that finest
  // part is an estimate (around Thanksgiving, the summer of 1944, the 1920s: printed "c.").
  // Saved as photokin does (a filled-in DateTimeOriginal at midnight plus a "DATE: Y!M~" keyword
  // saying which parts are known and which are guesses), and read back the same way.
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { post } from '../lib/api'
  import { dateState, isoFor, normDate, type DateLevel } from '../lib/metaedits'
  import Segmented from './Segmented.svelte'

  let { s }: { s: PhotoSession } = $props()

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
  const LEVELS: { value: DateLevel; label: string; tip: string }[] = [
    { value: 'day', label: 'Day', tip: 'The day is known (or, with Estimated, roughly: around Thanksgiving)' },
    { value: 'month', label: 'Month', tip: 'The month is known (or, with Estimated, roughly: the summer)' },
    { value: 'year', label: 'Year', tip: 'Only the year is known (or, with Estimated, roughly: the 1920s)' },
  ]

  const edited = $derived(s.draft.meta ? 'date' in s.draft.meta : false)
  const fileState = $derived(dateState(s.meta?.fields))
  const shown = $derived.by(() => {
    if (!edited) return fileState
    const d = s.draft.meta!.date
    return d ? ({ kind: 'date', ...normDate(d) } as const) : ({ kind: 'none' } as const)
  })

  // the inputs: filled from the date shown, kept while a part is still being typed
  let level = $state<DateLevel>('day')
  let estimate = $state(false)
  let year = $state<number | null>(null)
  let month = $state<number | null>(null)
  let day = $state<number | null>(null)
  let lastKey = ''
  $effect(() => {
    const key = JSON.stringify([s.path, shown])
    if (key === lastKey) return
    lastKey = key
    if (shown.kind === 'date') {
      const [y, m, d] = shown.iso.split('-').map(Number)
      level = shown.level
      estimate = shown.estimate
      year = y
      month = m || (level === 'day' || level === 'month' ? month : null)
      day = d || (level === 'day' ? day : null)
    } else if (shown.kind === 'none') {
      year = month = day = null
      estimate = false
    } else {
      year = shown.year
      month = day = null
      level = 'year'
    }
  })

  const iso = $derived(isoFor(level, year, month, day))
  const incomplete = $derived((year !== null || month !== null || day !== null) && !iso)
  // a half-typed date is not what a save would write: saving waits until it is whole (or cleared)
  $effect(() => {
    const session = s
    session.invalidDetail = incomplete ? 'Finish the date in the Metadata tab first (or clear it): the date shown is not complete.' : null
    return () => { session.invalidDetail = null }
  })

  function apply() {
    if (iso) app.setDate(s, { iso, level, estimate })
  }
  function setEstimate(v: boolean) {
    estimate = v
    apply()
  }
  function setLevel(v: string) {
    level = v as DateLevel
    if (level === 'day' && !day && month) day = 1
    if ((level === 'day' || level === 'month') && !month && year) month = 6
    apply()
  }
  function clear() {
    year = month = day = null
    app.setDate(s, null)
  }

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
  const daysIn = $derived(year && month ? new Date(year, month, 0).getDate() : 31)
</script>

<div class="datef col">
  {#if shown.kind === 'text' && !edited}
    <p class="small astext" data-tip="The file gives the date in words, so it is printed as written. Set a date to replace it.">“{shown.text}”</p>
  {/if}
  <div class="row wrap gap">
    <Segmented small label="How much of the date is known" value={level} options={LEVELS.map((l) => ({ value: l.value, label: l.label, tip: l.tip }))} onchange={setLevel} />
    <label class="row est" data-tip={level === 'day' ? 'A best guess at the day (around Thanksgiving: pick the 23rd). Printed “c. November 23, 1944”.' : level === 'month' ? 'A best guess at the month (the summer of 1944: pick July). Printed “c. July 1944”.' : 'A best guess at the year (the 1920s: pick 1925). Printed “c. 1925”.'}>
      <input type="checkbox" checked={estimate} onchange={(e) => setEstimate((e.target as HTMLInputElement).checked)} /> Estimated
    </label>
    {#if shown.kind !== 'none' || edited}
      <button class="btn sm ghost icon" aria-label="No date" data-tip="Remove the date (and its date keyword)" onclick={clear}>×</button>
    {/if}
  </div>
  <div class="row gap parts">
    <input class="field yr" type="number" min="1000" max={new Date().getFullYear() + 1} placeholder="Year" aria-label="Year" value={year ?? ''}
      step="1" oninput={(e) => { const v = (e.target as HTMLInputElement).value; year = v ? +v : null; if (year && year >= 1000) apply() }} />
    {#if level === 'day' || level === 'month'}
      <select class="field mo" aria-label="Month" value={month ?? ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; month = v ? +v : null; if (day && day > daysIn) day = daysIn; apply() }}>
        <option value="">Month</option>
        {#each MONTHS as m, i}<option value={i + 1}>{m}</option>{/each}
      </select>
    {/if}
    {#if level === 'day'}
      <input class="field dy" type="number" min="1" max={daysIn} step="1" placeholder="Day" aria-label="Day" value={day ?? ''}
        oninput={(e) => { const v = (e.target as HTMLInputElement).value; day = v ? +v : null; apply() }} />
    {/if}
  </div>
  {#if incomplete}
    <p class="small warnline">{level === 'day' ? 'Enter the year, month and day.' : level === 'month' ? 'Enter the year and month.' : 'Enter a year between 1000 and next year.'}
      {prints ? ` Until then the date stays ${prints}.` : ''}</p>
  {:else if prints}
    <p class="small faint">Prints as <span class="pv">{prints}</span></p>
  {/if}
</div>

<style>
  .datef { gap: 6px; min-width: 0; }
  .gap { gap: 6px; }
  .wrap { flex-wrap: wrap; }
  .parts .yr { width: 76px; }
  .parts .mo { flex: 1; min-width: 0; }
  .parts .dy { width: 58px; }
  .small { font-size: 12px; margin: 0; }
  .astext { color: var(--fg); font-style: italic; }
  .est { gap: 5px; font-size: 12px; color: var(--fg-2); cursor: pointer; }
  .warnline { color: var(--warn-fg); }
  .pv { color: var(--fg); }
</style>
