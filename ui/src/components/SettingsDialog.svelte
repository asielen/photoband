<script lang="ts">
  import { del, download, get, post } from '../lib/api'
  import { dialogs } from '../lib/dialogs.svelte'
  import { families, loadRegistry } from '../lib/fonts'
  import { app } from '../lib/store.svelte'
  import { batchRun } from '../lib/batchstate.svelte'
  import { patchLeaf, writeControl } from '../lib/controls'
  import type { Template } from '../lib/types'
  import Disclosure from './Disclosure.svelte'
  import FormatField from './FormatField.svelte'
  import Icon from './Icon.svelte'
  import Menu from './Menu.svelte'
  import Modal from './Modal.svelte'
  import Segmented from './Segmented.svelte'

  const tabs = [
    ['general', 'General', 'Default template, light or dark, and tooltips.'],
    ['formats', 'Caption formats', 'What each part of the caption says, filled in from each photo’s information.'],
    ['templates', 'Templates', 'Create, rename, import and export caption templates.'],
    ['saving', 'Saving', 'Where captioned copies go and what they are called.'],
    ['fonts', 'Fonts', 'Add fonts from a file or from Google Fonts.'],
    ['advanced', 'Advanced', 'Preview cache, ExifTool, logs and reset. Most people never need these.'],
  ] as const
  let tab = $state(dialogs.settingsOpen || 'general')
  const S = $derived(app.settings)

  /** Writes a settings patch. With the control's change event: afterwards the control shows what
   *  is really stored (the old value when the write failed, the cleaned value when the server
   *  corrected it), since a one-way bound control keeps the typed value otherwise. */
  function set(patch: any, e?: Event) {
    return writeControl(e, () => app.saveSettings(patch), () => patchLeaf(app.settings, patch), (er) => app.toast('error', er.message))
  }
  /** The default template also becomes the "last used" one, which new photos prefer. */
  function setDefault(id: string, e?: Event) {
    return set({ general: { defaultTemplate: id }, session: { lastTemplate: id } }, e)
  }
  function focusSelect(el: HTMLInputElement) {
    queueMicrotask(() => {
      el.focus()
      el.select()
    })
  }

  // ------------------------------------------------------------- templates / formats
  let editId = $state(app.session?.draft.templateId || app.defaultTemplateId())
  let work = $state<Template | null>(null)
  let dirty = $state(false)
  $effect(() => {
    const t = app.template(editId)
    if (t && (!work || work.id !== editId)) {
      work = JSON.parse(JSON.stringify(t))
      dirty = false
    }
  })
  let tryPath = $state(app.session?.path || app.photos[0]?.path || '')
  let tryFields = $state<Record<string, any> | null>(null)
  let tryRaw = $state<[string, string][]>([])
  let samples = $state<Record<string, any>>({})
  $effect(() => {
    const p = tryPath
    if (!p) return
    get('/api/photo/meta', { path: p }).then((m) => {
      tryFields = m.fields
      tryRaw = m.raw
    }).catch(() => (tryFields = null))
  })
  let sTimer: any
  $effect(() => {
    const w = work
    const f = tryFields
    if (!w || !f) return
    const formats: Record<string, string> = {}
    for (const b of w.blocks) formats[b.id] = b.format
    formats['__filename'] = S.saving.fileName
    clearTimeout(sTimer)
    sTimer = setTimeout(async () => {
      samples = await post('/api/resolve', { fields: f, formats, templateName: w.name }).catch(() => ({}))
    }, 120)
  })

  function touch() {
    dirty = true
  }
  function uniqueName(base: string) {
    const names = new Set(app.templates.map((t) => t.name.toLowerCase()))
    if (!names.has(base.toLowerCase())) return base
    let n = 2
    while (names.has(`${base} ${n}`.toLowerCase())) n++
    return `${base} ${n}`
  }
  const copyName = (name: string) => uniqueName(`${name} copy`)

  /** Unsaved caption-format edits: Save / Discard / Cancel. Resolves true when it's fine to move on. */
  async function guard(): Promise<boolean> {
    if (!dirty || !work) return true
    const r = await dialogs.ask(
      'Unsaved caption formats',
      `You changed the caption formats of “${work.name}”. Save the changes before leaving?`,
      [
        { id: 'cancel', label: 'Cancel' },
        { id: 'discard', label: 'Discard changes' },
        { id: 'save', label: work.builtin ? 'Save as my template' : 'Save', kind: 'primary' },
      ],
    )
    if (r.id === 'cancel') return false
    if (r.id === 'discard') {
      revert()
      return true
    }
    return await saveWork()
  }
  function revert() {
    const t = app.template(editId)
    work = t ? JSON.parse(JSON.stringify(t)) : null
    dirty = false
    saveAsName = null
  }
  async function closeSettings() {
    if (await guard()) dialogs.settingsOpen = null
  }
  async function switchTemplate(id: string, el?: HTMLSelectElement) {
    if (id === editId) return
    if (!(await guard())) {
      if (el) el.value = editId
      return
    }
    editId = id
    work = null
    saveAsName = null
  }

  // saving a built-in asks for the new template's name (inline, in the footer)
  let saveAsName = $state<string | null>(null)
  function offerUse(t: Template) {
    const s = app.session
    if (!s || s.draft.templateId === t.id) return
    app.toast('success', `Saved “${t.name}”.`, { label: 'Use it for this photo', run: () => app.setTemplate(s, t.id) }, 10000)
  }
  async function saveWork(): Promise<boolean> {
    if (!work) return false
    try {
      if (work.builtin) {
        const name = (saveAsName ?? '').trim() || copyName(work.name)
        const saved = await post<Template>('/api/templates', { template: { ...work, id: undefined, builtin: false, name }, new: true })
        await app.templatesChanged()
        editId = saved.id
        work = JSON.parse(JSON.stringify(saved))
        saveAsName = null
        dirty = false
        if (app.session && app.session.draft.templateId !== saved.id) offerUse(saved)
        else app.toast('success', `Built-in templates are read-only, so this was saved as “${saved.name}”.`)
      } else {
        const saved = await post<Template>('/api/templates', { template: work })
        await app.templatesChanged()
        work = JSON.parse(JSON.stringify(saved))
        dirty = false
        app.toast('success', `Saved “${saved.name}”.`)
      }
      return true
    } catch (e: any) {
      app.toast('error', `Could not save the template: ${e.message}`)
      return false
    }
  }
  function startSave() {
    if (!work) return
    if (work.builtin && saveAsName === null) {
      saveAsName = copyName(work.name)
      return
    }
    saveWork()
  }

  // new / duplicate ask for a name inline at the top of the Templates list
  let naming = $state<{ base: Template; text: string; title: string } | null>(null)
  function duplicate(t: Template) {
    naming = { base: t, text: copyName(t.name), title: `Duplicate “${t.name}” as` }
  }
  function newTemplate() {
    const base = app.template(S.general.defaultTemplate) || app.templates[0]
    naming = { base, text: uniqueName('New template'), title: `Name for the new template (a copy of “${base.name}”)` }
  }
  async function commitNaming() {
    if (!naming) return
    const { base, text } = naming
    const name = text.trim()
    if (!name) return
    naming = null
    try {
      const saved = await post<Template>('/api/templates', { template: { ...base, id: undefined, builtin: false, name }, new: true })
      await app.templatesChanged()
      app.toast('success', `Created “${saved.name}”.`)
    } catch (e: any) {
      app.toast('error', e.message)
    }
  }
  let renaming = $state<string | null>(null)
  let renameText = $state('')
  function rename(t: Template) {
    renaming = t.id
    renameText = t.name
  }
  async function commitRename(t: Template) {
    if (renaming !== t.id) return
    const name = renameText.trim()
    renaming = null
    if (!name || name === t.name) return
    await post('/api/templates', { template: { ...t, name } })
    await app.templatesChanged()
    if (editId === t.id && work) work.name = name
  }
  async function remove(t: Template) {
    if (!(await dialogs.confirm('Delete template', `Delete “${t.name}”? Photos that use it switch to the default template.`, 'Delete', true))) return
    await del(`/api/templates/${t.id}`)
    if (S.general.defaultTemplate === t.id || S.session.lastTemplate === t.id) {
      const fallback = app.templates.find((x) => x.id !== t.id && x.builtin)?.id || app.templates.find((x) => x.id !== t.id)?.id
      if (fallback) await setDefault(fallback)
    }
    await app.templatesChanged()
    if (editId === t.id) {
      editId = app.defaultTemplateId()
      work = null
      dirty = false
    }
  }
  function templateActions(t: Template) {
    const isDef = S.general.defaultTemplate === t.id
    return [
      { label: 'Duplicate…', run: () => duplicate(t) },
      ...(t.builtin ? [] : [{ label: 'Rename', run: () => rename(t) }]),
      { label: isDef ? 'Default template' : 'Set as default', disabled: isDef, run: () => setDefault(t.id) },
      { label: 'Export…', run: () => download(`/api/templates/${t.id}/export`, `${t.name.replace(/[^\w -]+/g, '')}.photoband-template.json`) },
      ...(t.builtin ? [] : [{ label: 'Delete…', sep: true, run: () => remove(t) }]),
    ]
  }
  let importInput = $state<HTMLInputElement>()!
  async function importFile(e: Event) {
    const f = (e.target as HTMLInputElement).files?.[0]
    if (!f) return
    try {
      const data = JSON.parse(await f.text())
      const t = await post<Template>('/api/templates/import', data)
      await app.templatesChanged()
      app.toast('success', `Imported “${t.name}”.`)
    } catch (err: any) {
      app.toast('error', `Import failed: ${err.message}`)
    }
    ;(e.target as HTMLInputElement).value = ''
  }
  function addBlock() {
    if (!work) return
    let n = work.blocks.length + 1
    let id = `block${n}`
    while (work.blocks.some((b) => b.id === id)) id = `block${++n}`
    const style = JSON.parse(JSON.stringify(work.blocks.at(-1)?.style ?? { font: 'source-serif-4', weight: 400, italic: false, size: 1.8, color: '#222222', align: 'left', lineHeight: 1.25, letterSpacing: 0, spaceBefore: 0, spaceAfter: 0, case: 'none' }))
    work.blocks.push({ id, name: `Block ${n}`, format: '', column: 0, style })
    touch()
  }
  function moveBlock(i: number, d: number) {
    if (!work) return
    const j = i + d
    if (j < 0 || j >= work.blocks.length) return
    const b = work.blocks.splice(i, 1)[0]
    work.blocks.splice(j, 0, b)
    touch()
  }

  // ------------------------------------------------------------- fonts
  let gCatalog = $state<{ family: string; category: string }[] | null>(null)
  let gQuery = $state('')
  let gBusy = $state('')
  let gError = $state('')
  const gShown = $derived((gCatalog || []).filter((f) => !gQuery || f.family.toLowerCase().includes(gQuery.toLowerCase())).slice(0, 60))
  async function browseGoogle() {
    gError = ''
    gBusy = 'catalog'
    try {
      gCatalog = await get('/api/fonts/google')
    } catch (e: any) {
      gError = e.message
    }
    gBusy = ''
  }
  function previewLink(fams: string[]) {
    const id = 'gf-preview'
    document.getElementById(id)?.remove()
    if (!fams.length) return
    const l = document.createElement('link')
    l.id = id
    l.rel = 'stylesheet'
    l.href = 'https://fonts.googleapis.com/css2?' + fams.map((f) => 'family=' + encodeURIComponent(f)).join('&') + '&text=' + encodeURIComponent(sampleText) + '&display=swap'
    document.head.appendChild(l)
  }
  /** A catalog family name as a CSS font-family value: only letters, digits and spaces survive. */
  function cssFamily(name: string): string {
    const safe = String(name || '').replace(/[^A-Za-z0-9 ]/g, '').trim()
    return safe ? `"${safe}", serif` : 'serif'
  }
  const sampleText = 'Picnic at Lake Merced, 1952 — Ann, Bea and Carl'
  $effect(() => {
    if (gCatalog) previewLink(gShown.map((f) => f.family))
  })
  async function gDownload(fam: string) {
    gBusy = fam
    try {
      await post('/api/fonts/google/download', { family: fam })
      await loadRegistry()
      app.fontsLoaded()
      app.toast('success', `${fam} added.`)
    } catch (e: any) {
      app.toast('error', e.message)
    }
    gBusy = ''
  }
  async function addFontFile() {
    const p = await dialogs.pick('open-font', 'Add a font file')
    if (!p?.length) return
    try {
      const r = await post('/api/fonts/add', { path: p[0] })
      await loadRegistry()
      app.fontsLoaded()
      app.toast('success', `${r.family} added.`)
    } catch (e: any) {
      app.toast('error', e.message)
    }
  }
  /** Returns true when a folder was chosen. Choosing a folder for copies also selects that option. */
  async function pickFolder(key: 'fixedFolder' | 'backupFolder'): Promise<boolean> {
    const cur = key === 'fixedFolder' ? S.saving.fixedFolder : S.saving.backupFolder
    const p = await dialogs.pick('open-folder', key === 'fixedFolder' ? 'Folder for copies' : 'Folder for backups', cur || S.session.lastFolder || '')
    if (!p?.length) return false
    await set({ saving: key === 'fixedFolder' ? { fixedFolder: p[0], location: 'fixed' } : { backupFolder: p[0] } })
    return true
  }
  /** The ExifTool program can only be chosen with the system file dialog; the server checks it
   *  runs as ExifTool and stores the path itself. */
  async function chooseExiftool() {
    try {
      const r = await post<{ paths?: string[]; unavailable?: boolean; settings?: any }>('/api/dialog', { kind: 'exiftool' })
      if (r.unavailable) {
        app.toast('error', 'The system file dialog is not available, so ExifTool cannot be chosen here. Set PHOTOBAND_EXIFTOOL instead.')
        return
      }
      if (r.settings) {
        app.settings = r.settings
        app.toast('success', 'ExifTool was changed.')
      }
    } catch (e: any) {
      app.toast('error', e.message)
    }
  }
  async function useBundledExiftool() {
    try {
      app.settings = await post('/api/exiftool/use-bundled')
    } catch (e: any) {
      app.toast('error', e.message)
    }
  }
  let locGroup = $state<HTMLDivElement>()
  /** Put the radio group back on the stored value (e.g. the folder pick was cancelled). */
  function syncLoc() {
    locGroup?.querySelectorAll<HTMLInputElement>('input[name=loc]').forEach((r) => (r.checked = r.value === S.saving.location))
  }
  async function chooseFixed() {
    if (S.saving.fixedFolder) await set({ saving: { location: 'fixed' } })
    else await pickFolder('fixedFolder')
    syncLoc()
  }
  const jpegPossible = $derived(S.saving.outputFormat === 'jpeg' || S.saving.outputFormat === 'same')
  const fontList = $derived.by(() => {
    void app.fontsVersion
    return families
  })
  const tipsOn = $derived(S.general.showTooltips !== false)
  /** The live example for a block: undefined (no line) when no photo is open to try it on. */
  function blockExample(id: string) {
    if (!app.photos.length) return undefined
    return samples[id] ?? null
  }
  // the same names as the font menu in the Style panel
  const SOURCE_NAMES: Record<string, string> = { bundled: 'Bundled', user: 'Your fonts', google: 'Google Fonts', system: 'Installed' }
  const backupWhere = $derived(S.saving.backupFolder || '“_originals” next to each photo')
  // Saving settings while something saves: a single save uses them as they are, so they wait for it;
  // a batch saves with the settings it was created with, so changes apply to the next batch
  const saveLock = $derived(app.saving ? 'Wait until the save finishes: it uses these settings as they are.' : '')
</script>

<Modal title="Settings" width={940} height="min(720px, calc(100vh - 48px))" noPad onclose={closeSettings}>
  <div class="layout">
    <nav aria-label="Settings sections">
      {#each tabs as [id, label, tip]}
        <button class:on={tab === id} aria-current={tab === id ? 'page' : undefined} data-tip={tip} data-tip-side="right" onclick={() => (tab = id)}>{label}</button>
      {/each}
    </nav>
    <div class="content scroll">
      {#if tab === 'general'}
        <h3>General</h3>
        <section class="grp">
          <h4>Captions</h4>
          <div class="set">
            <span class="k">Default template</span>
            <div class="v">
              <select class="field deftpl" aria-label="Default template" data-tip="The look new photos start with. You can switch any photo to another template in the toolbar." value={S.general.defaultTemplate} onchange={(e) => setDefault((e.target as HTMLSelectElement).value, e)}>
                {#each app.templates as t}<option value={t.id}>{t.name}</option>{/each}
              </select>
              <p class="desc">New photos start with this template.</p>
            </div>
          </div>
        </section>
        <section class="grp">
          <h4>Appearance</h4>
          <div class="set">
            <span class="k">Theme</span>
            <div class="v">
              <Segmented label="Theme" value={S.general.theme} options={[{ value: 'system', label: 'Match system', title: 'Follow your computer’s light or dark mode.' }, { value: 'light', label: 'Light', title: 'Always use light windows.' }, { value: 'dark', label: 'Dark', title: 'Always use dark windows.' }]} onchange={(v) => set({ general: { theme: v } })} />
              <p class="desc">Light or dark windows. The photo preview always sits on neutral grey so band colours look right.</p>
            </div>
          </div>
          <div class="set">
            <span class="k">Tooltips</span>
            <div class="v">
              <label class="switch" data-tip="Turn the short explanations that appear when you point at a control on or off.">
                <input type="checkbox" role="switch" checked={tipsOn} onchange={(e) => set({ general: { showTooltips: (e.target as HTMLInputElement).checked } }, e)} />
                <span class="track" aria-hidden="true"></span>
                Show tooltips
              </label>
              <p class="desc">Explain buttons and settings when you point at them or reach them with the Tab key.</p>
            </div>
          </div>
        </section>
        <Disclosure id="settings.general.advanced" label="Advanced" tip="Settings most people never change.">
          <section class="grp first">
            <h4>Measurements</h4>
            <div class="set">
              <span class="k">Show sizes as</span>
              <div class="v">
                <Segmented label="Display unit" value={S.general.displayUnit} options={[{ value: '%', label: '% of width', title: 'Sizes as a share of the photo’s width, so a template looks the same on any photo.' }, { value: 'px', label: 'px', title: 'Pixels of the saved file.' }, { value: 'pt', label: 'pt', title: 'Points (1/72 inch) at the photo’s print size.' }, { value: 'mm', label: 'mm', title: 'Millimetres at the photo’s print size.' }]} onchange={(v) => set({ general: { displayUnit: v } })} />
                <p class="desc">How sizes and spacing are shown in the Style and Layout panels. Only the display changes, not the caption.</p>
              </div>
            </div>
          </section>
        </Disclosure>
      {:else if tab === 'formats'}
        <h3>Caption formats</h3>
        <p class="lead">What each part of the caption says. Fields such as the date or people’s names are filled in from each photo’s information; anything else you type is kept as written.</p>
        <div class="pickers">
          <label class="pick"><span class="k">Template</span>
            <select class="field" value={editId} data-tip="The template whose caption formats you are editing." onchange={(e) => switchTemplate((e.target as HTMLSelectElement).value, e.target as HTMLSelectElement)} aria-label="Template to edit">
              {#each app.templates as t}<option value={t.id}>{t.name}{t.builtin ? '' : ' (yours)'}</option>{/each}
            </select>
          </label>
          <label class="pick"><span class="k">Example photo</span>
            <select class="field" bind:value={tryPath} aria-label="Photo to try the formats on" data-tip={app.photos.length ? 'The open photo used for the live examples under each format.' : 'Open some photos first to see live examples here.'} disabled={!app.photos.length}>
              {#if !app.photos.length}<option value="">Open some photos to see live examples</option>{/if}
              {#each app.photos as p}<option value={p.path}>{p.name}</option>{/each}
            </select>
          </label>
        </div>
        {#if work}
          {#if work.builtin}<div class="note">Built-in templates can’t be changed. When you save, your changes become your own copy.</div>{/if}
          <div class="blocks">
            {#each work.blocks as b, i (b.id)}
              <div class="blk">
                <div class="row">
                  <input class="field name" value={b.name} oninput={(e) => { b.name = (e.target as HTMLInputElement).value; touch() }} aria-label="Part name" />
                  {#if work.layout.columns.count === 2}
                    <Segmented small label="Column" value={String(b.column || 0)} options={[{ value: '0', label: 'Left column', title: 'Show this part in the left column of the band.' }, { value: '1', label: 'Right column', title: 'Show this part in the right column of the band.' }]} onchange={(v) => { b.column = +v; touch() }} />
                  {/if}
                  <span class="grow"></span>
                  <button class="btn ghost sm icon" aria-label="Move up" data-tip={i === 0 ? 'Already the first part of the caption.' : 'Move this part up in the caption.'} disabled={i === 0} onclick={() => moveBlock(i, -1)} style="transform:rotate(180deg)"><Icon name="chevdown" size={13} stroke={2} /></button>
                  <button class="btn ghost sm icon" aria-label="Move down" data-tip={i === work.blocks.length - 1 ? 'Already the last part of the caption.' : 'Move this part down in the caption.'} disabled={i === work.blocks.length - 1} onclick={() => moveBlock(i, 1)}><Icon name="chevdown" size={13} stroke={2} /></button>
                  <button class="btn ghost sm icon" aria-label="Delete part" data-tip={work.blocks.length < 2 ? 'A caption needs at least one part.' : 'Remove this part from the caption. Nothing is saved until you press Save.'} disabled={work.blocks.length < 2} onclick={() => { work!.blocks.splice(i, 1); touch() }}><Icon name="trash" size={13} /></button>
                </div>
                <FormatField value={b.format} label="{b.name} format" example={blockExample(b.id)} onchange={(v) => { b.format = v; touch() }} />
              </div>
            {/each}
            <button class="btn sm addblk" data-tip="Add another part to the caption, such as notes or a place." onclick={addBlock}><Icon name="plus" size={13} /> Add a part</button>
          </div>
          <Disclosure id="settings.formats.syntax" label="How formats work" tip="The format language, for when you want more control.">
            <ul class="help">
              <li><b>Fields:</b> <code>{'{title}'}</code> inserts a value; <code>{'{date:mmmm d, yyyy}'}</code> chooses how it is written; <code>{'{names|sep=; |last= & }'}</code> sets options.</li>
              <li><b>Optional text:</b> <code>[Taken {'{date:yyyy}'}]</code> is left out, words and all, when a field inside it is empty.</li>
              <li><b>Date parts:</b> yyyy yy mmmm mmm mm m dd d. Missing parts are dropped with their separators.</li>
              <li><b>Style:</b> <code>**bold**</code>, <code>*italic*</code>; a new line is a line break; <code>\{'{'}</code> <code>\[</code> <code>\*</code> are typed literally.</li>
              <li><b>Options:</b> case=upper|lower|title, max=N; names: sep, last, order=lr|rl|meta; rows: row_labels="Front: / Back: ", row_sep; keywords: sep, exclude="People,Scan".</li>
            </ul>
          </Disclosure>
          <Disclosure id="settings.formats.raw" label="All information found in the example photo" count={tryRaw.length ? `(${tryRaw.length})` : ''} tip="Every metadata value read from the example photo, to see why a field came out empty.">
            {#if tryRaw.length}
              <table class="raw">
                <tbody>{#each tryRaw as [k, v]}<tr><th>{k}</th><td>{v}</td></tr>{/each}</tbody>
              </table>
            {:else}<p class="desc">{app.photos.length ? 'No information was found in this photo.' : 'Open a photo to see its information here.'}</p>{/if}
          </Disclosure>
        {/if}
      {:else if tab === 'templates'}
        <div class="row head"><h3 class="grow">Templates</h3>
          <button class="btn sm" data-tip="Make a new template, starting from a copy of the default one." onclick={newTemplate}><Icon name="plus" size={13} /> New…</button>
          <button class="btn sm" data-tip="Add a template from a .json file someone exported." onclick={() => importInput.click()}><Icon name="upload" size={13} /> Import…</button>
          <input type="file" accept=".json,application/json" bind:this={importInput} onchange={importFile} hidden />
        </div>
        <p class="lead">A template is a caption’s look and wording. The one marked default is used for new photos; use ⋯ to duplicate, rename, export or delete.</p>
        {#if naming}
          <div class="naming row">
            <span class="muted">{naming.title}</span>
            <input class="field grow" use:focusSelect bind:value={naming.text} aria-label="New template name" onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); commitNaming() } if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); naming = null } }} />
            <button class="btn sm ghost" onclick={() => (naming = null)}>Cancel</button>
            <button class="btn sm primary" disabled={!naming.text.trim()} data-tip={naming.text.trim() ? 'Create the template.' : 'Type a name first.'} onclick={commitNaming}>Create</button>
          </div>
        {/if}
        <div class="tlist">
          {#each app.templates as t (t.id)}
            {@const isDef = S.general.defaultTemplate === t.id}
            <div class="trow row">
              <span class="star" class:on={isDef} aria-hidden="true">{isDef ? '★' : ''}</span>
              <div class="grow">
                <div>{#if renaming === t.id}<input class="field" use:focusSelect bind:value={renameText} onkeydown={(e) => { if (e.key === 'Enter') commitRename(t); if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); renaming = null } }} onblur={() => commitRename(t)} aria-label="Template name" />{:else}<b>{t.name}</b>{/if}{#if !t.builtin}<span class="tag" data-tip="A template you made. Built-in templates (unmarked) come with Photoband and can’t be changed.">yours</span>{/if}{#if isDef}<span class="tag def" data-tip="New photos start with this template.">default</span>{/if}</div>
                <div class="faint small">{t.description || `${t.blocks.length} part${t.blocks.length === 1 ? '' : 's'} · sizes follow ${t.scaleMode === 'physical' ? 'print size' : 'photo width'}`}</div>
              </div>
              <button class="btn sm ghost" data-tip="Change what each part of this template’s caption says." onclick={async () => { if (editId === t.id || (await guard())) { if (editId !== t.id) { editId = t.id; work = null } tab = 'formats' } }}>Edit formats</button>
              <Menu label="More actions for {t.name}" align="right" items={templateActions(t)}>
                {#snippet trigger()}<span class="more" aria-hidden="true">⋯</span>{/snippet}
              </Menu>
            </div>
          {/each}
        </div>
        <p class="faint small">Fonts, sizes and borders are changed on a photo (Style and Layout panels) and kept with “Save as template” or “Update template”. Exported templates include their wording but not font files.</p>
      {:else if tab === 'saving'}
        <h3>Saving</h3>
        {#if saveLock}<p class="desc warn" role="status">{saveLock}</p>
        {:else if batchRun.running}<p class="desc" role="status">A batch is saving with the settings it started with: changes here apply to the next batch and to single photos.</p>{/if}
        <!-- every control of this tab waits while a save runs -->
        <fieldset class="plain" disabled={!!saveLock}>
        <section class="grp">
          <h4>How saving works</h4>
          <ul class="how">
            <li><b>Save copy &amp; next</b>: the original is not touched. The captioned photo is saved as a new file, placed and named as set below.</li>
            <li><b>Overwrite &amp; next</b> with backups on: the photo is first copied to the backup folder ({backupWhere}), then the captioned photo replaces it. For a photo Photoband already captioned, its earlier backup of the untouched original is kept.</li>
            <li><b>Overwrite &amp; next</b> with backups off: the captioned photo replaces the file, which can’t be recovered.</li>
          </ul>
          <p class="desc">The strip under the toolbar shows the exact file each button writes for the open photo.</p>
        </section>
        <section class="grp">
          <h4>Backups (for Overwrite)</h4>
          <label class="row opt" data-tip={saveLock || 'Strongly recommended: each photo is copied to the backup folder before it is replaced, so it can be put back.'}><input type="checkbox" checked={S.saving.backupOriginals} onchange={(e) => set({ saving: { backupOriginals: (e.target as HTMLInputElement).checked } }, e)} /> Back up each photo before overwriting it</label>
          <div class="row small sub" class:off={!S.saving.backupOriginals}>Backups go to <span class="path" data-tip={backupWhere}>{backupWhere}</span> <button class="btn sm" data-tip="Pick one folder for all backups." disabled={!S.saving.backupOriginals} onclick={() => pickFolder('backupFolder')}>Choose…</button>{#if S.saving.backupFolder}<button class="btn sm ghost" data-tip="Keep each backup in an “_originals” folder next to its photo." onclick={() => set({ saving: { backupFolder: '' } })}>Use _originals</button>{/if}</div>
          {#if !S.saving.backupOriginals}<p class="desc warn">Without a backup, an overwritten photo can’t be restored.</p>{/if}
        </section>
        <section class="grp">
          <h4>Where copies go</h4>
          <div class="col" role="radiogroup" aria-label="Save copies to" bind:this={locGroup}>
            <label class="row opt" data-tip="The copy sits right beside the original, with a different name."><input type="radio" name="loc" value="same" checked={S.saving.location === 'same'} onchange={(e) => set({ saving: { location: 'same' } }, e)} /> In the same folder as the original</label>
            <label class="row opt" data-tip="Keeps copies tidy: each folder of photos gets its own subfolder of captioned copies."><input type="radio" name="loc" value="subfolder" checked={S.saving.location === 'subfolder'} onchange={(e) => set({ saving: { location: 'subfolder' } }, e)} /> In a subfolder next to the original, named <input class="field" style="width:140px" value={S.saving.subfolderName} aria-label="Subfolder name" onchange={(e) => set({ saving: { subfolderName: (e.target as HTMLInputElement).value.trim() || 'captioned' } }, e)} /></label>
            <div class="row opt"><label class="row" data-tip="Every captioned copy goes into one folder you choose."><input type="radio" name="loc" value="fixed" checked={S.saving.location === 'fixed'} onchange={chooseFixed} /> In one folder</label> <span class="path" data-tip={S.saving.fixedFolder || 'No folder chosen yet.'}>{S.saving.fixedFolder || 'none chosen'}</span> <button class="btn sm" data-tip="Pick the folder for all captioned copies." onclick={async () => { await pickFolder('fixedFolder'); syncLoc() }}>Choose…</button></div>
          </div>
          <p class="desc">Your original photos are never changed by Save copy.</p>
        </section>
        <section class="grp">
          <h4>File name</h4>
          <FormatField single rows={1} value={S.saving.fileName} extraTokens={['template']} label="File name pattern" example={app.photos.length ? (samples.__filename ?? null) : undefined} onchange={(v) => set({ saving: { fileName: v } })} />
          <p class="desc">The name of each copy. Fields like the original file name fill in for each photo; the extension is added for you.</p>
        </section>
        <Disclosure id="settings.saving.advanced" label="Advanced" tip="File type, name clashes, file dates and safety switches.">
          <section class="grp first">
            <h4>File type</h4>
            <div class="set">
              <span class="k">Save as</span>
              <div class="v">
                <div class="row">
                  <select class="field" aria-label="Output format" data-tip="The file type of captioned copies. “Same as the original” keeps TIFF as TIFF, JPEG as JPEG." value={S.saving.outputFormat} onchange={(e) => set({ saving: { outputFormat: (e.target as HTMLSelectElement).value } }, e)}>
                    <option value="same">Same as the original</option><option value="tiff">TIFF</option><option value="jpeg">JPEG</option><option value="png">PNG</option>
                  </select>
                  <label class="row" class:off={!jpegPossible} data-tip={jpegPossible ? 'Higher keeps more detail and makes bigger files. 95 is a good choice.' : 'Only used for JPEG files. Choose JPEG or “Same as the original” to change it.'}>JPEG quality <input class="field" type="number" min="50" max="100" style="width:64px" disabled={!jpegPossible} value={S.saving.jpegQuality} onchange={(e) => set({ saving: { jpegQuality: Math.max(50, Math.min(100, +(e.target as HTMLInputElement).value || 95)) } }, e)} /></label>
                </div>
                <p class="desc">TIFF and PNG copies keep the photo exactly; JPEG copies are smaller.</p>
              </div>
            </div>
            <div class="set">
              <span class="k">If the name is taken</span>
              <div class="v">
                <Segmented label="If the name exists" value={S.saving.onExists} options={[{ value: 'increment', label: 'Add -2, -3…', title: 'Keep both: the new copy gets a number added to its name.' }, { value: 'ask', label: 'Ask', title: 'Ask each time a copy with that name already exists.' }, { value: 'overwrite', label: 'Replace', title: 'Replace an earlier copy of the same photo. Files Photoband didn’t make are never replaced.' }]} onchange={(v) => set({ saving: { onExists: v } })} />
                <p class="desc">What happens when a copy with the same name is already there.</p>
              </div>
            </div>
            <h4>File details</h4>
            <label class="row opt" data-tip="Handy when your photo library sorts by file date."><input type="checkbox" checked={S.saving.keepFileDates} onchange={(e) => set({ saving: { keepFileDates: (e.target as HTMLInputElement).checked } }, e)} /> Give the copy the original’s modified date</label>
            <label class="row opt" data-tip="Stores only the caption text and band layout invisibly in the pixels (a band marker), never other photo information."><input type="checkbox" checked={S.saving.embedMarker} onchange={(e) => set({ saving: { embedMarker: (e.target as HTMLInputElement).checked } }, e)} /> Mark the band invisibly so Photoband recognizes it later</label>
            <p class="desc">The mark helps Photoband re-caption a photo even if another program removed its photo information.</p>
            <h4>Safety switches</h4>
            <label class="row opt" data-tip="Off by default: handwriting on an original print is part of the historical record."><input type="checkbox" checked={S.saving.allowOverwriteHandwritten} onchange={(e) => set({ saving: { allowOverwriteHandwritten: (e.target as HTMLInputElement).checked } }, e)} /> Allow overwriting scans that have a handwritten or printed caption</label>
            <label class="row opt" data-tip="A multi-page TIFF holds several images. Saving keeps only the first one, so this is off by default."><input type="checkbox" checked={S.saving.allowMultipageSave} onchange={(e) => set({ saving: { allowMultipageSave: (e.target as HTMLInputElement).checked } }, e)} /> Allow saving multi-page TIFFs (keeps only the first page)</label>
          </section>
        </Disclosure>
        </fieldset>
      {:else if tab === 'fonts'}
        <h3>Fonts</h3>
        <p class="lead">{fontList.length} font families are ready to use in captions. Add more from a font file on this computer or from Google Fonts.</p>
        <div class="row">
          <button class="btn sm" data-tip="Add a .ttf or .otf font file from this computer." onclick={addFontFile}><Icon name="plus" size={13} /> Add font file…</button>
          <button class="btn sm" data-tip={gBusy === 'catalog' ? 'Loading the Google Fonts list…' : 'Browse and download free fonts from Google Fonts. This needs an internet connection.'} onclick={browseGoogle} disabled={gBusy === 'catalog'}><Icon name="search" size={13} /> Browse Google Fonts</button>
        </div>
        {#if gCatalog || gError}
          <div class="gf">
            <div class="row"><input class="field grow" placeholder="Search Google Fonts" bind:value={gQuery} aria-label="Search Google Fonts" /><button class="btn sm ghost" data-tip="Close the Google Fonts list (and stop going online)." onclick={() => { gCatalog = null; gError = ''; previewLink([]) }}>Close</button></div>
            {#if gError}<div class="note err">{gError}</div>{/if}
            <p class="faint small">This is the only part of Photoband that goes online, and only while this list is open.</p>
            <div class="gflist scroll">
              {#each gShown as f}
                {@const have = families.some((x) => x.family === f.family)}
                <div class="row gfrow">
                  <div class="grow"><div class="small muted">{f.family} <span class="faint">{f.category}</span></div><div class="gsample" style:font-family={cssFamily(f.family)}>{sampleText}</div></div>
                  <button class="btn sm" disabled={!!gBusy || have} data-tip={have ? 'Already added.' : gBusy ? 'Wait for the current download to finish.' : `Download ${f.family} and add it to your fonts.`} onclick={() => gDownload(f.family)}>{have ? 'Added' : gBusy === f.family ? 'Downloading…' : 'Add'}</button>
                </div>
              {/each}
            </div>
          </div>
        {/if}
        <Disclosure id="settings.fonts.installed" label="Installed fonts" count={`(${fontList.length})`} tip="Every font Photoband can use, and where it came from.">
          <div class="row"><span class="grow faint small">Fonts added outside Photoband show up after a rescan.</span><button class="btn sm ghost" data-tip="Look for new or removed font files." onclick={async () => { await loadRegistry(true); app.fontsLoaded() }}>Rescan</button></div>
          <table class="fonts">
            <thead><tr><th>Family</th><th>Use</th><th>From</th><th>Styles</th></tr></thead>
            <tbody>
              {#each fontList as f (f.id)}
                <tr><td>{f.family}{#if f.fallback}<span class="tag" data-tip="Used for characters other fonts don’t have.">fallback</span>{/if}</td><td class="muted">{f.role}</td><td class="muted">{SOURCE_NAMES[f.source] || f.source}</td><td class="muted">{f.faces.length}{f.smallCaps ? ' · small caps' : ''}</td></tr>
              {/each}
            </tbody>
          </table>
        </Disclosure>
      {:else if tab === 'advanced'}
        <h3>Advanced</h3>
        <p class="lead">For troubleshooting and special setups. Most people never need these.</p>
        <section class="grp">
          <h4>Performance</h4>
          <div class="set">
            <span class="k">Preview cache</span>
            <div class="v">
              <label class="row" data-tip="Disk space kept for quick previews of large scans. Older previews are removed when it is full."><input class="field" type="number" min="100" step="100" style="width:90px" aria-label="Preview cache size in MB" value={S.advanced.cacheSizeMB} onchange={(e) => set({ advanced: { cacheSizeMB: Math.max(100, +(e.target as HTMLInputElement).value || 2048) } }, e)} /> MB</label>
              <p class="desc">More space makes reopening big scans faster.</p>
            </div>
          </div>
        </section>
        <section class="grp">
          <h4>Photo information</h4>
          <div class="set">
            <span class="k">ExifTool</span>
            <div class="v">
              <div class="row"><span class="path" data-tip={S.advanced.exiftoolPath || 'The copy of ExifTool that comes with Photoband.'}>{S.advanced.exiftoolPath || 'Bundled ExifTool'}</span> <button class="btn sm" data-tip="Use another installed copy of ExifTool." onclick={chooseExiftool}>Choose ExifTool…</button>{#if S.advanced.exiftoolPath}<button class="btn sm ghost" data-tip="Go back to the copy that comes with Photoband." onclick={useBundledExiftool}>Use bundled</button>{/if}</div>
              <p class="desc">The program that reads and writes photo information. Change it only if asked to.</p>
            </div>
          </div>
        </section>
        <section class="grp">
          <h4>Troubleshooting</h4>
          <div class="set">
            <span class="k">Logs</span>
            <div class="v">
              <div class="row"><button class="btn sm" data-tip="Show the folder with Photoband’s log files, to send with a problem report." onclick={() => post('/api/open-folder', { which: 'logs' })}>Open logs folder</button><button class="btn sm" data-tip="A list of every photo saved, with where it went." onclick={() => (dialogs.helpOpen = 'log')}>View save log</button></div>
              <p class="desc">Records of what Photoband did, for when something went wrong.</p>
            </div>
          </div>
          <div class="set">
            <span class="k">Reset</span>
            <div class="v">
              <button class="btn sm danger" style="justify-self:start" data-tip="Put every setting back to how it was when Photoband was installed. Asks first." onclick={async () => { if (await dialogs.confirm('Reset settings', 'Reset all settings to their defaults? Templates and fonts are kept.', 'Reset', true)) { app.settings = await post('/api/settings/reset'); app.applyTheme(); app.toast('success', 'Settings were reset to their defaults.') } }}>Reset to defaults…</button>
              <p class="desc">Your templates, fonts and photos are kept.</p>
            </div>
          </div>
        </section>
      {/if}
    </div>
  </div>
  {#snippet footer()}
    {#if (tab === 'formats') && work}
      {#if saveAsName !== null}
        <label class="row grow savename"><span class="muted">Save as</span><input class="field grow" use:focusSelect bind:value={saveAsName} aria-label="Name for your copy" onkeydown={(e) => { if (e.key === 'Enter') { e.preventDefault(); if (saveAsName?.trim()) saveWork() } if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); saveAsName = null } }} /></label>
        <button class="btn" onclick={() => (saveAsName = null)}>Cancel</button>
        <button class="btn primary" disabled={!saveAsName.trim()} data-tip={saveAsName.trim() ? 'Save these caption formats as your own template.' : 'Type a name for your template first.'} onclick={saveWork}>Save my template</button>
      {:else if dirty}
        <span class="grow faint small" role="status">Unsaved changes to “{work.name}”.</span>
        <button class="btn ghost" data-tip="Throw away the changes since the last save." onclick={revert}>Discard changes</button>
        <button class="btn" onclick={closeSettings}>Done</button>
        <button class="btn primary" data-tip={work.builtin ? 'Built-in templates can’t be changed, so this saves your own copy.' : 'Save the changes to this template.'} onclick={startSave}>{work.builtin ? 'Save as my template…' : 'Save template'}</button>
      {:else}
        <span class="grow faint small">Format changes are kept only when you save the template.</span>
        <button class="btn primary" onclick={closeSettings}>Done</button>
      {/if}
    {:else}
      <span class="grow faint small">Changes are saved as you make them.</span>
      <button class="btn primary" onclick={closeSettings}>Done</button>
    {/if}
  {/snippet}
</Modal>

<style>
  fieldset.plain { border: 0; padding: 0; margin: 0; min-width: 0; }
  .layout { display: grid; grid-template-columns: 172px 1fr; height: 100%; min-height: 0; }
  nav { border-right: 1px solid var(--line); padding: 12px 8px; display: flex; flex-direction: column; gap: 2px; background: var(--bg); }
  nav button { text-align: left; border: 0; background: none; padding: 0 10px; min-height: 30px; border-radius: 6px; cursor: pointer; color: var(--fg-2); }
  nav button:hover { background: var(--bg-hover); color: var(--fg); }
  nav button:focus-visible { outline: none; box-shadow: var(--focus); }
  nav button.on { background: var(--bg-active); color: var(--fg); font-weight: 600; }
  .content { padding: 16px 24px 24px; min-height: 0; }
  h3 { margin: 0 0 4px; font-size: 15px; }
  .head { margin-bottom: 4px; }
  .head h3 { margin: 0; }
  .lead { margin: 0 0 16px; color: var(--fg-2); font-size: 12.5px; line-height: 1.45; max-width: 72ch; }
  h3 + .grp { margin-top: 8px; }
  .grp { padding: 4px 0 8px; }
  .grp + .grp { border-top: 1px solid var(--line); margin-top: 8px; padding-top: 12px; }
  .grp.first { padding-top: 0; }
  h4 { margin: 4px 0 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--fg-3); }
  .grp h4 + .set, .grp h4 + .col, .grp h4 + label { margin-top: 0; }
  .set + h4, .desc + h4 { margin-top: 18px; }
  .set { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 4px 16px; align-items: start; margin-bottom: 14px; }
  .set .k { color: var(--fg-2); padding-top: 5px; }
  .v { min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 4px; }
  .desc { margin: 2px 0 0; font-size: 12px; color: var(--fg-3); line-height: 1.4; }
  .desc.warn { color: var(--warn-fg); }
  .opt { min-height: 28px; color: var(--fg); }
  .col { gap: 2px; }
  .switch { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; min-height: 28px; position: relative; }
  .switch input { position: absolute; opacity: 0; width: 34px; height: 20px; margin: 0; cursor: pointer; }
  .switch .track { width: 34px; height: 20px; border-radius: 10px; background: var(--line-2); position: relative; transition: background 0.15s; flex: none; }
  .switch .track::after { content: ''; position: absolute; top: 2px; left: 2px; width: 16px; height: 16px; border-radius: 50%; background: #fff; box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3); transition: transform 0.15s; }
  .switch input:checked + .track { background: var(--accent); }
  .switch input:checked + .track::after { transform: translateX(14px); }
  .switch input:focus-visible + .track { box-shadow: var(--focus); }
  @media (prefers-reduced-motion: reduce) { .switch .track, .switch .track::after { transition: none; } }
  .small { font-size: 12px; }
  .note { background: var(--info-bg); color: var(--info-fg); padding: 8px 10px; border-radius: 6px; font-size: 12.5px; margin-bottom: 12px; }
  .note.err { background: var(--err-bg); color: var(--err-fg); }
  .pickers { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 12px; }
  .pick { display: flex; align-items: center; gap: 8px; flex: 1 1 220px; min-width: 0; }
  .pick .k { color: var(--fg-2); white-space: nowrap; }
  .pick select { flex: 1; min-width: 0; }
  .blocks { display: flex; flex-direction: column; gap: 12px; }
  .blk { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px; background: var(--bg); }
  .blk .name { width: 180px; font-weight: 600; }
  .addblk { align-self: flex-start; }
  .help { margin: 0; padding-left: 18px; font-size: 12.5px; line-height: 1.55; color: var(--fg-2); }
  .help code { font: 11.5px var(--font-mono); background: var(--bg-3); padding: 0 3px; border-radius: 3px; }
  .raw { width: 100%; font: 11px var(--font-mono); border-collapse: collapse; }
  .raw th { text-align: left; font-weight: 500; color: var(--fg-2); width: 40%; padding: 2px 8px 2px 0; vertical-align: top; }
  .raw td { word-break: break-word; }
  .tlist { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 10px; }
  .trow { padding: 10px 12px; gap: 4px; }
  .trow + .trow { border-top: 1px solid var(--line); }
  .tag { font-size: 10.5px; margin-left: 6px; padding: 0 6px; border-radius: 8px; background: var(--bg-3); color: var(--fg-2); border: 1px solid var(--line); }
  .tag.def { background: var(--info-bg); color: var(--info-fg); border-color: transparent; }
  .star { width: 16px; flex: none; color: var(--warn, #d99a00); font-size: 13px; text-align: center; }
  .more { font-size: 16px; line-height: 1; letter-spacing: 1px; padding: 0 2px; }
  .naming { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; background: var(--bg); gap: 8px; }
  .deftpl { width: 280px; max-width: 100%; }
  .sub { padding-left: 24px; min-height: 28px; color: var(--fg-2); }
  .off { opacity: 0.55; }
  .savename { gap: 8px; }
  .path { font: 11.5px var(--font-mono); color: var(--fg-2); background: var(--bg-3); padding: 1px 6px; border-radius: 4px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fonts { width: 100%; border-collapse: collapse; font-size: 12.5px; margin-top: 6px; }
  .fonts th { text-align: left; color: var(--fg-2); font-weight: 600; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  .fonts td { padding: 5px 8px; border-bottom: 1px solid var(--line); }
  .gf { border: 1px solid var(--line); border-radius: 8px; padding: 10px; margin: 12px 0; }
  .gflist { max-height: 300px; margin-top: 6px; }
  .gfrow { padding: 6px 2px; border-top: 1px solid var(--line); }
  .gsample { font-size: 20px; line-height: 1.3; }
  .how { margin: 0 0 4px; padding-left: 18px; font-size: 13px; line-height: 1.5; }
  .how li { margin: 2px 0; }
</style>
