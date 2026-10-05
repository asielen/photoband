<script lang="ts">
  import { families, hasItalic, weightsFor, family as famOf } from '../lib/fonts'
  import { app, type PhotoSession } from '../lib/store.svelte'
  import BlockEditor from './BlockEditor.svelte'
  import ColorField from './ColorField.svelte'
  import Disclosure from './Disclosure.svelte'
  import Icon from './Icon.svelte'
  import NumField from './NumField.svelte'
  import Segmented from './Segmented.svelte'
  import { dialogs } from '../lib/dialogs.svelte'
  import { actions } from '../lib/actions'
  import { post } from '../lib/api'
  import { dateRowText } from '../lib/datetext'

  let { s }: { s: PhotoSession } = $props()

  const base = $derived(app.template(s.draft.templateId))
  const eff = $derived.by(() => {
    void s.draft.overrides
    return app.effective(s)
  })
  const photoW = $derived(s.layout?.sourceRect[2] ?? s.meta?.info.upright_width ?? 1000)
  const dpi = $derived(s.meta?.info.dpi?.[0] ?? null)
  const unit = $derived(app.settings.general.displayUnit)
  const mode = $derived(eff?.scaleMode ?? 'relative')
  let styleBlock = $state<string>('')
  $effect(() => {
    if (eff && !eff.blocks.find((b) => b.id === styleBlock)) styleBlock = eff.blocks[0]?.id ?? ''
  })
  const blk = $derived(eff?.blocks.find((b) => b.id === styleBlock))
  const baseBlk = $derived(base?.blocks.find((b) => b.id === styleBlock))

  const lowConf = $derived.by(() => {
    const ex = s.existing
    if (!ex?.blocks || s.draft.existingChoice !== 'recognized') return []
    const out: string[] = []
    for (const b of ex.blocks) for (const l of b.lines) for (const w of l.words || []) if (w.confidence < 0.6 && w.text.trim()) out.push(w.text.replace(/[.,;:!?]+$/, ''))
    return out
  })

  function bo(key: string): boolean {
    return (s.draft.overrides?.blocks?.[styleBlock] as any)?.[key] !== undefined
  }
  function lo(path: string): boolean {
    let o: any = s.draft.overrides?.layout
    for (const p of path.split('.')) o = o?.[p]
    return o !== undefined
  }
  function setB(patch: Record<string, any>) {
    app.setStyle(s, styleBlock, patch)
  }
  function setL(patch: Record<string, any>) {
    app.setLayout(s, patch)
  }
  function resetB(key: string) {
    app.clearOverride(s, 'block', key, styleBlock)
  }
  function resetL(key: string) {
    app.clearOverride(s, 'layout', key)
  }

  // "More options" shows a dot while closed when something inside differs from the template
  const styleAdvChanged = $derived.by(() => {
    void s.draft.overrides
    void styleBlock
    return ['weight', 'italic', 'lineHeight', 'letterSpacing', 'spaceBefore', 'spaceAfter', 'case'].some((k) => bo(k))
  })
  const layoutAdvChanged = $derived.by(() => {
    void s.draft.overrides
    return s.draft.overrides?.scaleMode !== undefined || ['bandHeight.overflow', 'padding', 'textMaxWidth', 'vAlign', 'columns', 'divider', 'keyline'].some((k) => lo(k))
  })
  // a warning about a control behind "More options" opens it: never hide what needs fixing
  const warns = $derived.by(() => {
    void s.layout
    void s.meta
    return app.warnings(s)
  })
  const styleForce = $derived(warns.some((w) => w.kind === 'font' && /small capitals|no bold|no italic/.test(w.message)))
  const layoutForce = $derived(warns.some((w) => w.kind === 'overflow' || (w.kind === 'small' && !w.block)))
  const noOverrides = $derived(!app.hasOverrides(s))
  const NO_CHANGES = 'Change a style or layout setting first. Changes made here apply to this photo only.'
  const noFaces = $derived(!!s.meta && s.meta.faces.named.length === 0 && s.meta.faces.unnamed_count === 0)
  const TABS = [
    ['text', 'Text', 'Edit the caption text'],
    ['style', 'Style', 'Font, size, colour and alignment of each caption line'],
    ['layout', 'Layout', 'Border sizes, band height and colours'],
    ['metadata', 'Metadata', 'The details found in the file (read-only)'],
  ] as const

  // tab pattern: one tab stop, arrows / Home / End move between tabs
  function tabKey(e: KeyboardEvent) {
    const i = TABS.findIndex(([id]) => id === app.inspectorTab)
    let j = -1
    if (e.key === 'ArrowRight') j = (i + 1) % TABS.length
    else if (e.key === 'ArrowLeft') j = (i - 1 + TABS.length) % TABS.length
    else if (e.key === 'Home') j = 0
    else if (e.key === 'End') j = TABS.length - 1
    if (j < 0) return
    e.preventDefault()
    e.stopPropagation()
    const list = e.currentTarget as HTMLElement
    app.inspectorTab = TABS[j][0]
    queueMicrotask(() => list.querySelectorAll<HTMLButtonElement>('[role=tab]')[j]?.focus())
  }

  const fontGroups = $derived.by(() => {
    void app.fontsVersion
    const g: Record<string, typeof families> = { bundled: [], user: [], google: [], system: [] }
    for (const f of families) if (!f.fallback) g[f.source]?.push(f)
    return g
  })

  async function saveAsTemplate() {
    const r = await dialogs.ask('Save as template', `Save this photo's style and layout changes as a new template based on “${base?.name}”?`, [
      { id: 'cancel', label: 'Cancel' },
      { id: 'ok', label: 'Save new template', kind: 'primary' },
    ])
    if (r.id === 'ok') await app.saveOverridesAsTemplate(s, true)
  }
  async function updateTemplate() {
    if (!base || base.builtin) return
    const ok = await dialogs.confirm('Update template', `Change “${base.name}” for every photo that uses it?`, 'Update template')
    if (ok) await app.saveOverridesAsTemplate(s, false)
  }

  const facesLR = $derived.by(() => {
    const f = s.meta?.faces
    if (!f) return []
    const named = f.named.slice()
    if (named.every((x) => x.box)) named.sort((a, b) => a.box![0] + a.box![2] / 2 - (b.box![0] + b.box![2] / 2))
    return named
  })
  let rawFilter = $state('')
  /** "(Binary data 1161 bytes, use -b option to extract)" → "binary · 1.1 KB" */
  function humanize(v: string): string {
    const m = /^\(Binary data (\d+) bytes/.exec(String(v))
    if (!m) return v
    const n = +m[1]
    return `binary · ${n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`}`
  }
  const raw = $derived((s.meta?.raw || []).map(([k, v]) => [k, humanize(v)] as [string, string]).filter(([k, v]) => !rawFilter || (k + ' ' + v).toLowerCase().includes(rawFilter.toLowerCase())))
  // the dates as captions print them ({date}, {digitized}), not the raw metadata values
  let dateText = $state('')
  let scanText = $state('')
  $effect(() => {
    const f = s.meta?.fields
    dateText = ''
    scanText = ''
    if (!f?.date && !f?.digitized) return
    let live = true
    post<Record<string, { plain?: string; text?: string }>>('/api/resolve', { fields: f, formats: { date: '{date}', digitized: '{digitized}' } })
      .then((r) => {
        if (!live) return
        dateText = r.date?.plain || r.date?.text || ''
        scanText = r.digitized?.plain || r.digitized?.text || ''
      })
      .catch(() => {})
    return () => { live = false }
  })
  const fieldRows = $derived.by(() => {
    const f = s.meta?.fields || {}
    const src = s.meta?.fieldSources || {}
    return [
      ['Title', f.title, src.title],
      ['Caption', f.caption, src.caption],
      ['Notes', f.notes, src.notes],
      ['Date', dateRowText(dateText, f.date), src.date],
      ['Scan date', dateRowText(scanText, f.digitized), src.digitized],
      ['Creator', f.creator, src.creator],
      ['Location', [f.sublocation, f.city, f.state, f.country].filter(Boolean).join(', '), src.city || src.sublocation],
      ['Keywords', (f.keywords || []).join(', '), ''],
    ] as [string, string, string][]
  })
</script>

<aside class="insp">
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <div class="tabs" role="tablist" aria-label="Caption panel" onkeydown={tabKey}>
    {#each TABS as [id, label, tip]}
      <button role="tab" id="insp-tab-{id}" aria-selected={app.inspectorTab === id} aria-controls="insp-panel" tabindex={app.inspectorTab === id ? 0 : -1} class:on={app.inspectorTab === id} data-tip={tip} onclick={() => (app.inspectorTab = id)}>{label}</button>
    {/each}
  </div>

  <div class="body scroll" id="insp-panel" role="tabpanel" aria-labelledby="insp-tab-{app.inspectorTab}">
    {#if !eff}
      <p class="faint pad">This photo’s template is missing. Choose another one from Template in the toolbar.</p>
    {:else if app.inspectorTab === 'text'}
      <div class="col pad stack">
        {#each eff.blocks as b (b.id)}
          <BlockEditor {s} block={b} lowConfidence={lowConf} />
        {/each}
        <div class="row quiet">
          <label class="row check" class:off={noFaces} data-tip={noFaces ? 'This photo has no tagged faces to show.' : 'Outline each tagged face with its name and left-to-right number. Never saved.'} data-tip-key="F">
            <input type="checkbox" bind:checked={app.showFaces} disabled={noFaces} /> Show faces
          </label>
          <span class="grow"></span>
          <button class="btn sm ghost" disabled={!app.hasEdits(s)} data-tip={app.hasEdits(s) ? "Discard this photo's text and style edits. You can undo this." : 'Nothing to revert: this photo follows its template.'} onclick={actions.revertToTemplate}><Icon name="reset" size={13} /> Revert to template</button>
        </div>
      </div>
    {:else if app.inspectorTab === 'style'}
      <div class="col pad stack">
        {#if eff.blocks.length > 1}
          <Segmented label="Caption line" value={styleBlock} options={eff.blocks.map((b) => ({ value: b.id, label: b.name, tip: `Style the ${b.name} line` }))} onchange={(v) => (styleBlock = v)} small />
        {/if}
        {#if blk && baseBlk}
          <div class="grid">
            <span class="k">Font</span>
            <span class="row">
              <select class="field grow" value={blk.style.font} onchange={(e) => setB({ font: (e.target as HTMLSelectElement).value })} aria-label="Font" data-tip="Typeface for this line. Add your own in Settings › Fonts.">
                {#if !famOf(blk.style.font)}<option value={blk.style.font}>{blk.style.font} (missing)</option>{/if}
                {#each [['bundled', 'Bundled'], ['user', 'Your fonts'], ['google', 'Google Fonts'], ['system', 'Installed']] as [k, lbl]}
                  {#if fontGroups[k]?.length}
                    <optgroup label={lbl}>
                      {#each fontGroups[k] as f}<option value={f.id}>{f.family}</option>{/each}
                    </optgroup>
                  {/if}
                {/each}
              </select>
              {#if bo('font')}<button class="dot" data-tip="Changed for this photo. Click to use the template's font." aria-label="Reset font" onclick={() => resetB('font')}></button>{/if}
            </span>
            <span class="k">Size</span>
            <NumField value={blk.style.size} kind="font" {mode} displayUnit={unit} {photoW} {dpi} min={0.05} step={0.1} changed={bo('size')} onreset={() => resetB('size')} onchange={(v) => setB({ size: v })} a11yLabel="Size" />
            <span class="k">Colour</span>
            <ColorField a11yLabel="Text" value={blk.style.color} changed={bo('color')} onreset={() => resetB('color')} onchange={(v) => setB({ color: v })} />
            <span class="k">Align</span>
            <span class="row">
              <Segmented value={blk.style.align} small options={[{ value: 'left', label: 'Left', tip: 'Align left' }, { value: 'center', label: 'Center', tip: 'Center the text' }, { value: 'right', label: 'Right', tip: 'Align right' }, { value: 'justify', label: 'Justify', tip: 'Stretch full lines to both edges' }]} onchange={(v) => setB({ align: v })} label="Alignment" />
              {#if bo('align')}<button class="dot" data-tip="Changed for this photo. Click to use the template's alignment." aria-label="Reset alignment" onclick={() => resetB('align')}></button>{/if}
            </span>
          </div>
          <Disclosure id="style-more" changed={styleAdvChanged} force={styleForce} tip="Weight, line height, spacing and capitals">
            <div class="grid">
              <span class="k" data-tip="How heavy the letters are. 400 is regular, 700 is bold.">Weight</span>
              <span class="row">
                <select class="field" value={blk.style.weight} onchange={(e) => setB({ weight: +(e.target as HTMLSelectElement).value })} aria-label="Weight" data-tip="Letter weight. Only the weights this font has are listed.">
                  {#each weightsFor(blk.style.font) as w}<option value={w}>{w}</option>{/each}
                  {#if !weightsFor(blk.style.font).includes(blk.style.weight)}<option value={blk.style.weight}>{blk.style.weight} (n/a)</option>{/if}
                </select>
                <label class="row check" data-tip={hasItalic(blk.style.font) ? 'Set the whole line in italics' : 'This font has no italic style.'}><input type="checkbox" checked={blk.style.italic} disabled={!hasItalic(blk.style.font)} onchange={(e) => setB({ italic: (e.target as HTMLInputElement).checked })} /> Italic</label>
                {#if bo('weight') || bo('italic')}<button class="dot" data-tip="Changed for this photo. Click to use the template's weight." aria-label="Reset weight" onclick={() => { resetB('weight'); resetB('italic') }}></button>{/if}
              </span>
              <span class="k" data-tip="Distance between lines, as a multiple of the text size">Line height</span>
              <NumField value={blk.style.lineHeight} unit="×" min={0.6} max={4} step={0.05} changed={bo('lineHeight')} onreset={() => resetB('lineHeight')} onchange={(v) => setB({ lineHeight: v })} a11yLabel="Line height" tip="Distance between lines, as a multiple of the text size" />
              <span class="k" data-tip="Extra space between letters, as a % of the text size">Letter spacing</span>
              <NumField value={blk.style.letterSpacing} unit="%" min={-5} max={30} step={0.5} changed={bo('letterSpacing')} onreset={() => resetB('letterSpacing')} onchange={(v) => setB({ letterSpacing: v })} a11yLabel="Letter spacing" tip="Extra space between letters, as a % of the text size" />
              <span class="k" data-tip="Extra space above this line of the caption">Space before</span>
              <NumField value={blk.style.spaceBefore} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={bo('spaceBefore')} onreset={() => resetB('spaceBefore')} onchange={(v) => setB({ spaceBefore: v })} a11yLabel="Space before" />
              <span class="k" data-tip="Extra space below this line of the caption">Space after</span>
              <NumField value={blk.style.spaceAfter} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={bo('spaceAfter')} onreset={() => resetB('spaceAfter')} onchange={(v) => setB({ spaceAfter: v })} a11yLabel="Space after" />
              <span class="k" data-tip="As typed, all capitals, all lowercase or small capitals">Capitals</span>
              <span class="row">
                <select class="field" value={blk.style.case} onchange={(e) => setB({ case: (e.target as HTMLSelectElement).value })} aria-label="Case" data-tip="How capital letters are shown. Small capitals need a font that has them.">
                  <option value="none">As typed</option>
                  <option value="upper">UPPERCASE</option>
                  <option value="lower">lowercase</option>
                  <option value="smallcaps" disabled={!famOf(blk.style.font)?.smallCaps}>Small caps{famOf(blk.style.font)?.smallCaps ? '' : ' (not in this font)'}</option>
                </select>
                {#if bo('case')}<button class="dot" data-tip="Changed for this photo. Click to use the template's capitals." aria-label="Reset case" onclick={() => resetB('case')}></button>{/if}
              </span>
            </div>
          </Disclosure>
        {/if}
        <p class="hint">Changes apply to this photo only and are marked with a blue dot <span class="dot inline" aria-hidden="true"></span>. Save them as a template to reuse them.</p>
      </div>
    {:else if app.inspectorTab === 'layout'}
      {@const L = eff.layout}
      <div class="col pad stack">
        {#if s.draft.mode === 'erase'}
          <div class="note">Erase in place keeps the original band. Borders don't apply; padding places the new text inside the detected band.</div>
        {/if}
        <section class="col">
          <h3 class="section-h">Border</h3>
          <div class="grid2">
            <NumField label="Top" value={L.border.top} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('border.top')} onreset={() => resetL('border.top')} onchange={(v) => setL({ border: { top: v } })} />
            <NumField label="Bottom" value={L.border.bottom} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('border.bottom')} onreset={() => resetL('border.bottom')} onchange={(v) => setL({ border: { bottom: v } })} tip="Bottom border, where the caption sits" />
            <NumField label="Left" value={L.border.left} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('border.left')} onreset={() => resetL('border.left')} onchange={(v) => setL(L.lockSides ? { border: { left: v, right: v } } : { border: { left: v } })} />
            <NumField label="Right" value={L.lockSides ? L.border.left : L.border.right} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} disabled={L.lockSides} changed={lo('border.right')} onreset={() => resetL('border.right')} onchange={(v) => setL({ border: { right: v } })} tip={L.lockSides ? 'Follows Left. Turn off “Keep left and right equal” to set it on its own.' : ''} />
          </div>
          <label class="row check" data-tip="Changing Left changes Right too"><input type="checkbox" checked={L.lockSides} onchange={(e) => setL({ lockSides: (e.target as HTMLInputElement).checked, border: { right: L.border.left } })} /> Keep left and right equal</label>
        </section>
        <section class="col">
          <h3 class="section-h">Band</h3>
          <div class="row wrap">
            <Segmented value={L.bandHeight.mode} small options={[{ value: 'auto', label: 'Auto height', tip: 'The band grows to fit the caption' }, { value: 'fixed', label: 'Fixed height', tip: 'The band is the bottom border; long text shrinks to fit' }]} onchange={(v) => setL({ bandHeight: { mode: v } })} label="Band height" />
            {#if L.bandHeight.mode === 'auto'}
              <NumField label="Min" value={L.bandHeight.min} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} width={68} changed={lo('bandHeight.min')} onreset={() => resetL('bandHeight.min')} onchange={(v) => setL({ bandHeight: { min: v } })} tip="Smallest band height, even for a short caption" />
            {/if}
          </div>
          <div class="colors">
            <ColorField label="Border" value={L.borderColor} changed={lo('borderColor')} onreset={() => resetL('borderColor')} onchange={(v) => setL(L.linkColors ? { borderColor: v, bandColor: v } : { borderColor: v })} />
            <ColorField label="Band" value={L.bandColor} changed={lo('bandColor')} onreset={() => resetL('bandColor')} onchange={(v) => setL(L.linkColors ? { borderColor: v, bandColor: v } : { bandColor: v })} />
          </div>
          <label class="row check" data-tip="Changing one colour changes the other too"><input type="checkbox" checked={L.linkColors} onchange={(e) => setL({ linkColors: (e.target as HTMLInputElement).checked })} /> Same colour for border and band</label>
        </section>
        <Disclosure id="layout-more" changed={layoutAdvChanged} force={layoutForce} tip="Padding, text placement, columns, lines and print scale">
          {#if L.bandHeight.mode === 'fixed'}
            <section class="col">
              <h3 class="section-h">When the text doesn't fit</h3>
              <Segmented value={L.bandHeight.overflow} small options={[{ value: 'shrink', label: 'Shrink to fit', tip: 'Make the text smaller, down to 60% of its size' }, { value: 'warn', label: 'Warn', tip: 'Keep the size and show a warning' }]} onchange={(v) => setL({ bandHeight: { overflow: v } })} label="Overflow" />
            </section>
          {/if}
          <section class="col">
            <h3 class="section-h">Text in the band</h3>
            <div class="grid2">
              <NumField label="Pad top" value={L.padding.top} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('padding.top')} onreset={() => resetL('padding.top')} onchange={(v) => setL({ padding: { top: v } })} tip="Space between the photo and the text" />
              <NumField label="Pad bottom" value={L.padding.bottom} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('padding.bottom')} onreset={() => resetL('padding.bottom')} onchange={(v) => setL({ padding: { bottom: v } })} tip="Space between the text and the bottom edge" />
              <NumField label="Pad left" value={L.padding.left} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('padding.left')} onreset={() => resetL('padding.left')} onchange={(v) => setL({ padding: { left: v } })} tip="Space between the left edge and the text" />
              <NumField label="Pad right" value={L.padding.right} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('padding.right')} onreset={() => resetL('padding.right')} onchange={(v) => setL({ padding: { right: v } })} tip="Space between the text and the right edge" />
            </div>
            <div class="lrow">
              <NumField label="Text width" value={L.textMaxWidth} unit="%" min={10} max={100} step={1} changed={lo('textMaxWidth')} onreset={() => resetL('textMaxWidth')} onchange={(v) => setL({ textMaxWidth: v })} tip="Longest line, as a % of the band width" />
            </div>
            <div class="lrow row">
              <span class="lk" data-tip="Where the text sits when the band is taller than the text">Vertical</span>
              <Segmented value={L.vAlign} small options={[{ value: 'top', label: 'Top', tip: 'Text at the top of the band' }, { value: 'middle', label: 'Middle', tip: 'Text in the middle of the band' }, { value: 'bottom', label: 'Bottom', tip: 'Text at the bottom of the band' }]} onchange={(v) => setL({ vAlign: v })} label="Vertical position of the text" />
            </div>
          </section>
          <section class="col">
            <h3 class="section-h">Columns</h3>
            <div class="row wrap">
              <Segmented value={String(L.columns.count)} small options={[{ value: '1', label: 'One', tip: 'All caption lines in one column' }, { value: '2', label: 'Two', tip: 'Split the caption lines into a left and a right column' }]} onchange={(v) => setL({ columns: { count: +v } })} label="Columns" />
              {#if L.columns.count === 2}
                <NumField label="Split" value={L.columns.split} unit="%" min={10} max={90} step={1} changed={lo('columns.split')} onreset={() => resetL('columns.split')} onchange={(v) => setL({ columns: { split: v } })} tip="Width of the left column, as a % of the band" />
              {/if}
            </div>
            {#if L.columns.count === 2}
              {#each eff.blocks as b}
                <div class="row"><span class="grow">{b.name}</span>
                  <Segmented value={String(b.column || 0)} small options={[{ value: '0', label: 'Left', tip: `${b.name} in the left column` }, { value: '1', label: 'Right', tip: `${b.name} in the right column` }]} onchange={(v) => app.setStyle(s, b.id, { column: +v })} label="Column for {b.name}" />
                </div>
              {/each}
            {/if}
          </section>
          <section class="col">
            <h3 class="section-h">Lines</h3>
            <label class="row check" data-tip="A thin rule between the photo and the caption"><input type="checkbox" checked={L.divider.enabled} onchange={(e) => setL({ divider: { enabled: (e.target as HTMLInputElement).checked } })} /> Hairline divider above the band</label>
            {#if L.divider.enabled}
              <div class="row wrap sub">
                <NumField label="Width" value={L.divider.width} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0.01} step={0.01} changed={lo('divider.width')} onreset={() => resetL('divider.width')} onchange={(v) => setL({ divider: { width: v } })} tip="Thickness of the divider" />
                <NumField label="Inset" value={L.divider.inset} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0} changed={lo('divider.inset')} onreset={() => resetL('divider.inset')} onchange={(v) => setL({ divider: { inset: v } })} tip="Gap between the ends of the divider and the band edges" />
                <ColorField label="Colour" a11yLabel="Divider" value={L.divider.color} changed={lo('divider.color')} onreset={() => resetL('divider.color')} onchange={(v) => setL({ divider: { color: v } })} />
              </div>
            {/if}
            <label class="row check" data-tip="A thin outline around the photo itself"><input type="checkbox" checked={L.keyline.enabled} onchange={(e) => setL({ keyline: { enabled: (e.target as HTMLInputElement).checked } })} /> Keyline around the photo</label>
            {#if L.keyline.enabled}
              <div class="row wrap sub">
                <NumField label="Width" value={L.keyline.width} kind="len" {mode} displayUnit={unit} {photoW} {dpi} min={0.01} step={0.01} changed={lo('keyline.width')} onreset={() => resetL('keyline.width')} onchange={(v) => setL({ keyline: { width: v } })} tip="Thickness of the keyline" />
                <ColorField label="Colour" a11yLabel="Keyline" value={L.keyline.color} changed={lo('keyline.color')} onreset={() => resetL('keyline.color')} onchange={(v) => setL({ keyline: { color: v } })} />
              </div>
            {/if}
          </section>
          <section class="col">
            <h3 class="section-h">Sizes follow</h3>
            <Segmented value={mode} small options={[{ value: 'relative', label: 'Photo width', tip: 'Sizes are a share of the photo width: the same look at any resolution' }, { value: 'physical', label: 'Print size', tip: 'Sizes in points and millimetres at the file’s DPI: the same printed size on any print' }]} onchange={(v) => app.setScaleMode(s, v as any)} label="Scale mode" />
          </section>
        </Disclosure>
      </div>
    {:else}
      <div class="col pad stack">
        <section class="col">
          <h3 class="section-h">Details</h3>
          <dl class="kv">
            {#each fieldRows as [k, v, src]}
              <div class="kvrow" data-tip={src ? `Read from the file's ${src} field` : undefined} data-tip-side="left"><dt>{k}</dt><dd class:faint={!v}>{v || '—'}</dd></div>
            {/each}
          </dl>
        </section>
        <section class="col">
          <h3 class="section-h">People, left to right</h3>
          {#if facesLR.length}
            <ol class="faces">
              {#each facesLR as f}<li data-tip={f.box ? `Tagged on a face in the photo (${f.source})` : `Named without a face position (${f.source}), so its place in the order may be wrong`} data-tip-side="left">{f.name}{#if !f.box}<span class="faint"> · no position</span>{/if}</li>{/each}
            </ol>
          {:else}
            <p class="faint small">No named faces.</p>
          {/if}
          {#if s.meta?.faces.unnamed_count}<p class="faint small">{s.meta.faces.unnamed_count} face{s.meta.faces.unnamed_count > 1 ? 's' : ''} without a name.</p>{/if}
        </section>
        {#if s.meta}
          <section class="col">
            <h3 class="section-h">File</h3>
            <p class="small muted">{s.meta.info.format}, {s.meta.info.bits}-bit {s.meta.info.mode}{s.meta.info.compression !== 'none' && s.meta.info.format === 'TIFF' ? `, ${s.meta.info.compression.toUpperCase()}` : ''}, {s.meta.info.upright_width} × {s.meta.info.upright_height} px{s.meta.info.dpi ? `, ${Math.round(s.meta.info.dpi[0])} dpi` : ''}{s.meta.info.icc ? ', ICC profile' : ''}{s.meta.info.orientation !== 1 ? `, EXIF orientation ${s.meta.info.orientation}` : ''}</p>
            {#each s.meta.info.notes as n}<p class="faint small">{n}</p>{/each}
          </section>
        {/if}
        <Disclosure id="meta-all" label="All metadata" tip="Every value found in the file, with its technical name">
          <input class="field filter" placeholder="Filter" bind:value={rawFilter} aria-label="Filter metadata" />
          <dl class="raw">
            {#each raw as [k, v]}<div class="rawrow"><dt>{k}</dt><dd>{v}</dd></div>{/each}
          </dl>
        </Disclosure>
      </div>
    {/if}
  </div>

  {#if eff && (app.inspectorTab === 'style' || app.inspectorTab === 'layout')}
    <div class="foot row">
      <button class="btn sm ghost" disabled={noOverrides} data-tip={noOverrides ? NO_CHANGES : "Keep this photo's style and layout as a new template"} data-tip-side="top" onclick={saveAsTemplate}>Save as template</button>
      <button class="btn sm ghost" disabled={noOverrides || !!base?.builtin} onclick={updateTemplate} data-tip-side="top" data-tip={base?.builtin ? `“${base.name}” is built in and can’t be changed. Use Save as template to keep these changes.` : noOverrides ? NO_CHANGES : `Change “${base?.name}” for every photo that uses it`}>Update template</button>
      <span class="grow"></span>
      <button class="btn sm ghost" disabled={noOverrides} data-tip-side="top" data-tip={noOverrides ? 'Nothing to reset: this photo uses the template as it is.' : "Undo all of this photo's style and layout changes"} onclick={() => { s.draft.overrides = {}; app.commit(s) }}>Reset all</button>
    </div>
  {/if}
</aside>

<style>
  /* 340 px minimum: every Style and Layout control fits without horizontal scrolling at the 960 px window */
  .insp { width: clamp(340px, 28vw, 400px); container-type: inline-size; container-name: insp; flex: none; display: flex; flex-direction: column; background: var(--bg); border-left: 1px solid var(--line); min-height: 0; }
  .tabs { display: flex; padding: 8px 12px 0; gap: 4px; border-bottom: 1px solid var(--line); flex: none; }
  .tabs button { border: 0; background: none; padding: 8px 8px 9px; margin-bottom: -1px; color: var(--fg-2); font-weight: 500; border-bottom: 2px solid transparent; cursor: pointer; transition: color var(--t-fast) ease, border-color var(--t-fast) ease; }
  .tabs button:hover { color: var(--fg); }
  .tabs button.on { color: var(--fg); border-bottom-color: var(--accent); }
  .body { flex: 1; min-height: 0; overflow-x: hidden; }
  .pad { padding: 16px; }
  .stack { gap: 20px; }
  section.col { gap: 8px; }
  .grid { display: grid; grid-template-columns: 92px minmax(0, 1fr); gap: 8px 12px; align-items: center; }
  .grid > :global(*) { min-width: 0; }
  .grid2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .grid2 > :global(*) { min-width: 0; }
  /* fixed label column so the fields line up */
  .grid2 :global(.lbl) { width: 72px; flex: none; color: var(--fg-2); font-size: 12px; }
  .lrow :global(.lbl), .lk { width: 72px; flex: none; color: var(--fg-2); font-size: 12px; }
  .colors { display: flex; flex-direction: column; gap: 8px; }
  /* a narrower inspector than designed for: one field per row instead of two */
  @container insp (max-width: 359px) { .grid2 { grid-template-columns: minmax(0, 1fr); } }
  .k { color: var(--fg-2); font-size: 12px; }
  .check { gap: 8px; color: var(--fg-2); font-size: 12.5px; min-height: 28px; }
  .check.off { opacity: 0.55; }
  .quiet { margin-top: -8px; }
  .sub { padding-left: 24px; }
  .small { font-size: 12px; margin: 0; }
  .hint { font-size: 12px; color: var(--fg-3); margin: 0; line-height: 1.5; }
  .wrap { flex-wrap: wrap; }
  /* 7 px dot, 16 px hit area */
  .dot { width: 16px; height: 16px; border-radius: 50%; background: transparent; border: 0; padding: 0; cursor: pointer; flex: none; display: inline-grid; place-items: center; margin: 0 -4px; }
  .dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-link); transition: transform var(--t-fast) ease; }
  .dot:hover::before { transform: scale(1.3); }
  .dot.inline { width: 7px; height: 7px; margin: 0 1px; vertical-align: middle; cursor: default; display: inline-block; background: var(--accent-link); }
  .dot.inline::before { display: none; }
  .foot { padding: 6px 8px; border-top: 1px solid var(--line); gap: 2px; flex-wrap: wrap; flex: none; }
  .foot .btn { padding: 0 6px; }
  .note { background: var(--info-bg); color: var(--info-fg); padding: 8px 12px; border-radius: var(--radius); font-size: 12px; }
  .kv { margin: 0; font-size: 12.5px; display: flex; flex-direction: column; }
  .kvrow { display: grid; grid-template-columns: 84px minmax(0, 1fr); gap: 12px; padding: 4px 0; }
  .kv dt { color: var(--fg-2); }
  .kv dd { margin: 0; word-break: break-word; user-select: text; }
  /* key above value: long ExifTool names and values both get the full width */
  .raw { margin: 0; font-size: 11px; }
  .rawrow { padding: 4px 0; }
  .rawrow + .rawrow { border-top: 1px solid var(--line); }
  .raw dt { font: 10.5px var(--font-mono); color: var(--fg-3); word-break: break-all; }
  .raw dd { margin: 1px 0 0; font: 11.5px var(--font-mono); color: var(--fg); word-break: break-word; user-select: text; }
  .faces { margin: 0; padding-left: 20px; font-size: 12.5px; display: flex; flex-direction: column; gap: 2px; }
  .filter { width: 100%; }
</style>
