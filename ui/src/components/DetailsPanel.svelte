<script lang="ts">
  // The photo's details, editable: what captions are made from. Edits go into the photo you save
  // (a copy or the original); "Save to original" writes them into the photo itself on their own.
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { post } from '../lib/api'
  import { dateRowText } from '../lib/datetext'
  import { detailProblem, editCount, type TextField } from '../lib/metaedits'
  import DateField from './DateField.svelte'
  import KeywordField from './KeywordField.svelte'
  import PeopleList from './PeopleList.svelte'

  let { s }: { s: PhotoSession } = $props()

  const n = $derived(editCount(s.draft.meta))
  const src = $derived(s.meta?.fieldSources || {})
  const name = $derived(s.meta?.name ?? '')
  const backupOn = $derived(app.settings.saving.backupOriginals !== false)

  // where each detail is written (the first tag; mirrors that the file already has follow it)
  const WRITES: Record<string, string> = {
    title: 'XMP Title (and IPTC Object Name / Windows Title when the file has them)',
    caption: 'XMP Description (and IPTC Caption / EXIF Image Description when the file has them)',
    notes: 'EXIF User Comment, where photokin keeps its notes',
    creator: 'XMP Creator (and IPTC By-line / EXIF Artist when the file has them)',
    sublocation: 'XMP Location (IPTC Sub-location)',
    city: 'XMP City (IPTC City)',
    state: 'XMP State (IPTC Province-State)',
    country: 'XMP Country (IPTC Country)',
    keywords: 'XMP Subject (and IPTC Keywords when the file has them)',
    date: 'EXIF Date/Time Original and XMP Date Created, with a “DATE:” keyword saying how much of it is known',
  }
  const TEXT: { key: TextField; label: string; multi?: boolean; placeholder?: string }[] = [
    { key: 'title', label: 'Title' },
    { key: 'caption', label: 'Description', multi: true },
    { key: 'notes', label: 'Notes', multi: true, placeholder: 'Notes kept with the photo' },
    { key: 'creator', label: 'Photographer' },
  ]
  const PLACE: { key: TextField; label: string }[] = [
    { key: 'sublocation', label: 'Location' },
    { key: 'city', label: 'City' },
    { key: 'state', label: 'State or province' },
    { key: 'country', label: 'Country' },
  ]

  const edited = (k: string) => !!s.draft.meta && k in s.draft.meta
  const value = (k: TextField) => (edited(k) ? (s.draft.meta as any)[k] ?? '' : app.fileDetail(s, k))
  function tip(k: string, label: string) {
    const from = src[k] ? `Read from the file’s ${src[k]}. ` : ''
    return `${from}Saved to ${WRITES[k]}.` + (edited(k) ? ` Edited: click the dot to go back to the file’s ${label.toLowerCase()}.` : '')
  }
  // clearing a value the file has removes it from every tag captions read it from
  const clearNote = (k: TextField) => (edited(k) && !value(k).trim() && app.fileDetail(s, k) ? `The file’s ${src[k] || 'value'} is removed when saved.` : '')

  // the scan date (read-only), as captions print it
  let scanText = $state('')
  $effect(() => {
    const f = s.meta?.fields
    scanText = ''
    if (!f?.digitized) return
    let live = true
    post<Record<string, { plain?: string }>>('/api/resolve', { fields: f, formats: { d: '{digitized}' } }).then((r) => { if (live) scanText = r.d?.plain || '' }).catch(() => {})
    return () => { live = false }
  })

  const canWrite = $derived(!app.batchReview && !app.saving && !!s.meta && n > 0)
  const saveTip = $derived(app.batchReview ? 'In batch review, edits are saved with Save all.'
    : `Write only these details into ${name}; the photo itself is not changed or re-encoded.` + (backupOn ? ' With Backup on, the photo is backed up first if it has no backup yet.' : ''))
  function autosize(el: HTMLTextAreaElement, _value?: string) {
    const fit = () => { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight + 2, 220) + 'px' }
    fit()
    el.addEventListener('input', fit)
    return { update: fit, destroy: () => el.removeEventListener('input', fit) }
  }
</script>

<div class="details col">
  {#if n}
    <div class="bar" role="status">
      <div class="col grow">
        <b>{n} detail{n > 1 ? 's' : ''} changed</b>
        <span class="small">Saved into the photo with your next save, or now into the original on its own.</span>
      </div>
      <div class="row acts">
        <button class="btn sm primary" disabled={!canWrite} data-tip={saveTip} onclick={() => app.saveDetails(s)}>Save to original</button>
        <button class="btn sm ghost" data-tip="Undo all detail edits for this photo (Ctrl+Z brings them back)" onclick={() => app.discardDetails(s)}>Discard</button>
      </div>
    </div>
  {/if}

  <section class="col form">
    <h3 class="section-h">Details</h3>
    {#each TEXT as f (f.key)}
      <div class="fld">
        <div class="lab row"><label for="d-{f.key}" data-tip={tip(f.key, f.label)} data-tip-side="left">{f.label}</label>
          {#if edited(f.key)}<button class="dot" aria-label={`Undo the edit to ${f.label}`} data-tip="Edited. Click to go back to the file’s value." onclick={() => app.resetDetail(s, f.key)}></button>{/if}</div>
        {#if f.multi}
          <textarea id="d-{f.key}" class="field" rows="2" maxlength="65536" use:autosize={value(f.key)} value={value(f.key)} placeholder={f.placeholder ?? ''}
            oninput={(e) => app.setDetail(s, f.key, (e.target as HTMLTextAreaElement).value)}></textarea>
        {:else}
          <input id="d-{f.key}" class="field" maxlength="2000" value={value(f.key)} oninput={(e) => app.setDetail(s, f.key, (e.target as HTMLInputElement).value)} />
        {/if}
        {#if edited(f.key) && detailProblem(value(f.key))}<p class="small err">{detailProblem(value(f.key))}</p>{/if}
        {#if clearNote(f.key)}<p class="small faint">{clearNote(f.key)}</p>{/if}
      </div>
    {/each}

    <div class="fld">
      <div class="lab row"><span data-tip={tip('date', 'Date')} data-tip-side="left">Date</span>
        {#if edited('date')}<button class="dot" aria-label="Undo the date edit" data-tip="Edited. Click to go back to the file’s date." onclick={() => app.setDate(s, undefined)}></button>{/if}</div>
      <DateField {s} />
    </div>

    <div class="fld">
      <div class="lab row"><span>Place</span>
        {#if PLACE.some((p) => edited(p.key))}<button class="dot" aria-label="Undo the place edits" data-tip="Edited. Click to go back to the file’s place." onclick={() => PLACE.forEach((p) => edited(p.key) && app.resetDetail(s, p.key))}></button>{/if}</div>
      <div class="place">
        {#each PLACE as p (p.key)}
          <input class="field" class:ed={edited(p.key)} maxlength="2000" placeholder={p.label} aria-label={p.label} data-tip={tip(p.key, p.label)} value={value(p.key)}
            oninput={(e) => app.setDetail(s, p.key, (e.target as HTMLInputElement).value)} />
        {/each}
      </div>
    </div>

    <div class="fld">
      <div class="lab row"><span data-tip={tip('keywords', 'Keywords')} data-tip-side="left">Keywords</span>
        {#if edited('keywords')}<button class="dot" aria-label="Undo the keyword edits" data-tip="Edited. Click to go back to the file’s keywords." onclick={() => app.resetDetail(s, 'keywords')}></button>{/if}</div>
      <KeywordField {s} />
    </div>

    <div class="fld ro">
      <div class="lab"><span data-tip={src.digitized ? `When the photo was scanned or the file was made. Read from ${src.digitized}.` : 'When the photo was scanned or the file was made.'} data-tip-side="left">Scan date</span></div>
      <p class="small" class:faint={!s.meta?.fields?.digitized}>{dateRowText(scanText, s.meta?.fields?.digitized) || '—'}</p>
    </div>
  </section>

  <PeopleList {s} />
</div>

<style>
  .details { gap: 20px; }
  .bar { position: sticky; top: 0; z-index: 2; margin: -16px -16px 0; padding: 10px 16px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; background: var(--info-bg); color: var(--info-fg); border-bottom: 1px solid var(--line); }
  .bar .grow { flex: 1; min-width: 160px; gap: 2px; }
  .acts { gap: 4px; }
  .form { gap: 10px; }
  .fld { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .lab { gap: 6px; align-items: center; font-size: 12px; color: var(--fg-2); min-height: 16px; }
  .lab label, .lab span { cursor: default; }
  .fld .field { width: 100%; }
  textarea.field { resize: none; overflow-y: auto; line-height: 1.4; }
  .place { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
  .place .ed { border-color: color-mix(in srgb, var(--accent-link) 55%, var(--line-2)); }
  .small { font-size: 12px; margin: 0; }
  .ro p { color: var(--fg); }
  .err { color: var(--err-fg); }
  .dot { width: 16px; height: 16px; border-radius: 50%; background: transparent; border: 0; padding: 0; cursor: pointer; flex: none; display: inline-grid; place-items: center; }
  .dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-link); transition: transform var(--t-fast) ease; }
  .dot:hover::before { transform: scale(1.3); }
  @container insp (max-width: 359px) { .place { grid-template-columns: minmax(0, 1fr); } }
</style>
