<script lang="ts">
  import { get } from '../lib/api'
  import { dialogs } from '../lib/dialogs.svelte'
  import { actions } from '../lib/actions'
  import { app } from '../lib/store.svelte'
  import Icon from './Icon.svelte'
  import Modal from './Modal.svelte'

  const modKey = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl'
  const pages = [
    ['start', 'Getting started'],
    ['shortcuts', 'Shortcuts'],
    ['log', 'Save log'],
    ['about', 'About'],
  ] as const
  const titles: Record<string, string> = { start: 'Getting started', shortcuts: 'Keyboard shortcuts', log: 'Save log', about: 'About Photoband' }
  const which = $derived(pages.some(([id]) => id === dialogs.helpOpen) ? dialogs.helpOpen! : 'start')
  let tabsEl = $state<HTMLDivElement>()
  function tabKey(e: KeyboardEvent) {
    const i = pages.findIndex(([id]) => id === which)
    let j = -1
    if (e.key === 'ArrowRight') j = (i + 1) % pages.length
    else if (e.key === 'ArrowLeft') j = (i - 1 + pages.length) % pages.length
    else if (e.key === 'Home') j = 0
    else if (e.key === 'End') j = pages.length - 1
    if (j < 0) return
    e.preventDefault()
    e.stopPropagation()
    dialogs.helpOpen = pages[j][0]
    queueMicrotask(() => tabsEl?.querySelectorAll<HTMLButtonElement>('[role=tab]')[j]?.focus())
  }
  const startSteps: { icon: string; title: string; text: string; key?: string[] }[] = [
    { icon: 'folder', title: 'Open your photos', text: 'Choose Open, then a folder or a few photos. They appear in the list on the left.', key: [modKey, 'Shift', 'O'] },
    { icon: 'pen', title: 'Check the caption', text: 'The caption is filled in from each photo’s information. Click the caption in the preview, or type in the panel on the right, to change it.', key: ['E'] },
    { icon: 'save', title: 'Save a copy', text: 'Save copy & next writes a captioned copy next to the original and opens the next photo. Your original photo is never changed by it.', key: [modKey, 'S'] },
    { icon: 'batch', title: 'Many photos? Use Batch', text: 'Batch captions a whole folder in three steps: choose the photos, check them, save.' },
  ]
  function openFolder() {
    dialogs.helpOpen = null
    actions.openFolder()
  }
  const mod = navigator.platform.toLowerCase().includes('mac') ? '⌘' : 'Ctrl'
  // A shortcut is a list of alternatives; each alternative is a list of keys pressed together.
  type Combo = string[]
  // Every keyboard shortcut in the app (App.svelte, Preview.svelte, Filmstrip.svelte, BlockEditor.svelte).
  // Tab and Shift+Tab always move focus; they are never taken over.
  const groups: { name: string; rows: [Combo[], string][] }[] = [
    {
      name: 'File',
      rows: [
        [[[mod, 'O']], 'Open files'],
        [[[mod, 'Shift', 'O']], 'Open folder'],
        [[[mod, 'S'], [mod, 'Enter']], 'Save a copy and go to the next photo'],
        [[[mod, 'Shift', 'S']], 'Save copy as…'],
        [[[mod, 'Shift', 'Enter']], 'Overwrite the original and go to the next photo'],
      ],
    },
    {
      name: 'Edit',
      rows: [
        [[['E'], ['Enter']], 'Start typing in the first caption block (from the photo list or preview)'],
        [[['Esc']], 'Leave the caption block you are typing in'],
        [[['Enter']], 'New line, while typing in a caption block'],
        [[[mod, 'Z']], 'Undo (text, style and layout, per photo)'],
        [[[mod, 'Shift', 'Z'], [mod, 'Y']], 'Redo'],
        [[[mod, 'B'], [mod, 'I']], 'Bold / italic in a text block'],
        [[['Arrow keys']], 'Edge tool: nudge the selected photo edge by 1 px'],
        [[['Shift', 'Arrow keys']], 'Edge tool: nudge the selected photo edge by 10 px'],
      ],
    },
    {
      name: 'Photos',
      rows: [
        [[['↑'], ['←'], ['PageUp']], 'Previous photo'],
        [[['↓'], ['→'], ['PageDown']], 'Next photo'],
        [[['Home'], ['End']], 'First / last photo (in the photo list)'],
        [[['PageUp'], ['PageDown']], 'Five photos back / forward (in the photo list)'],
      ],
    },
    {
      name: 'View',
      rows: [
        [[['Space']], 'Hold to show the original in the After pane'],
        [[['F']], 'Show faces and their left-to-right order'],
        [[['Y']], 'Switch Before / After between side by side and stacked'],
        [[[mod, '\\']], 'Hide or show the photo list and the caption panel'],
        [[[mod, '0']], 'Fit to window'],
        [[[mod, '1']], '100% zoom'],
        [[['Scroll wheel'], ['Drag']], 'Zoom at the pointer / pan (both panes stay in sync)'],
      ],
    },
    {
      name: 'App',
      rows: [
        [[['Tab'], ['Shift', 'Tab']], 'Move between buttons, fields and panels'],
        [[[mod, 'K']], 'Command palette: find any action by name'],
        [[[mod, ',']], 'Settings'],
        [[['F1'], ['?']], 'This list'],
        [[['Esc']], 'Close a dialog or menu'],
      ],
    },
    {
      name: 'Caption formats (Settings)',
      rows: [
        [[['{']], 'List the fields you can insert, such as the date or names'],
        [[['↑'], ['↓']], 'Move through the list of fields'],
        [[['Enter'], ['Tab']], 'Insert the highlighted field'],
      ],
    },
    {
      name: 'Choosing files',
      rows: [
        [[['Enter']], 'Open the highlighted folder, or choose the highlighted file'],
        [[['Backspace'], ['Alt', '←']], 'Up one folder'],
        [[[mod, 'Click'], ['Shift', 'Click']], 'Pick several files'],
      ],
    },
  ]
  const licenseName: Record<string, string> = { OFL: 'SIL OFL 1.1', APACHE2: 'Apache 2.0', 'APACHE-2.0': 'Apache 2.0', UFL: 'Ubuntu Font Licence 1.0' }
  let log = $state<any[]>([])
  const MODE: Record<string, string> = { copy: 'Saved a copy', copyAs: 'Saved a copy as', overwrite: 'Overwrote the original' }
  /** The hidden band data, in words: it lets Photoband re-edit the caption later. */
  function markerText(m: any): string {
    if (m?.robust) return m.payload ? 'hidden band data added' : 'hidden band mark added (without the caption text)'
    return m?.reason ? `no hidden band data (${m.reason})` : 'no hidden band data'
  }
  let lic = $state<any[]>([])
  $effect(() => {
    if (which === 'log') get('/api/log').then((l) => (log = l))
    if (which === 'about') get('/api/licenses').then((l) => (lic = l))
  })
</script>

<Modal title={titles[which]} width={720} height="min(720px, calc(100vh - 48px))" onclose={() => (dialogs.helpOpen = null)}>
  <!-- svelte-ignore a11y_interactive_supports_focus -->
  <div class="tabs" role="tablist" aria-label="Help pages" bind:this={tabsEl} onkeydown={tabKey}>
    {#each pages as [id, label]}
      <button role="tab" id="help-tab-{id}" aria-selected={which === id} aria-controls="help-panel" tabindex={which === id ? 0 : -1} data-autofocus={which === id ? '' : undefined} class:on={which === id} onclick={() => (dialogs.helpOpen = id)}>{label}</button>
    {/each}
  </div>
  <div id="help-panel" role="tabpanel" aria-labelledby="help-tab-{which}">
    {#if which === 'start'}
      <p class="intro">Photoband adds a Polaroid-style caption band under your photos. Four steps:</p>
      <ol class="start">
        {#each startSteps as st, i}
          <li>
            <span class="sicon" aria-hidden="true"><Icon name={st.icon} size={20} /></span>
            <div class="grow">
              <div class="stitle"><span class="snum">{i + 1}</span> {st.title}</div>
              <div class="stext">{st.text}</div>
              {#if i === 0}<button class="btn sm" data-tip="Choose a folder of photos to start." data-tip-key="Mod+Shift+O" onclick={openFolder}><Icon name="folder" size={14} /> Open a folder…</button>{/if}
            </div>
            {#if st.key}<span class="keyset skey">{#each st.key as k, ki}{#if ki}<span class="plus">+</span>{/if}<kbd>{k}</kbd>{/each}</span>{/if}
          </li>
        {/each}
      </ol>
      <div class="tips">
        <div class="tip"><Icon name="info" size={16} /><span>Point at any button to see what it does. Turn these tips off in Settings › General.</span></div>
        <div class="tip"><Icon name="undo" size={16} /><span>Every change can be undone with <kbd>{modKey}</kbd><span class="plus">+</span><kbd>Z</kbd>, so it is safe to explore.</span></div>
        <div class="tip"><Icon name="search" size={16} /><span>Press <kbd>{modKey}</kbd><span class="plus">+</span><kbd>K</kbd> to find any command, or <kbd>F1</kbd> for all shortcuts.</span></div>
      </div>
    {:else if which === 'shortcuts'}
      <table class="keys">
        {#each groups as g}
          <tbody>
            <tr class="grp"><th colspan="2" scope="colgroup">{g.name}</th></tr>
            {#each g.rows as [alts, d]}
              <tr>
                <td class="combo">
                  {#each alts as combo, ai}{#if ai}<span class="or">or</span>{/if}<span class="keyset">{#each combo as k, ki}{#if ki}<span class="plus">+</span>{/if}<kbd>{k}</kbd>{/each}</span>{/each}
                </td>
                <td>{d}</td>
              </tr>
            {/each}
          </tbody>
        {/each}
      </table>
    {:else if which === 'log'}
      {#if !log.length}<p class="faint">Nothing saved yet. Every photo you save is listed here, with where it went.</p>{/if}
      <div class="log">
        {#each log as e}
          <div class="entry" class:bad={!e.ok}>
            <div class="row"><b aria-label={e.ok ? 'Saved' : 'Failed'}>{e.ok ? '✓' : '✕'}</b><span class="grow path">{e.output || e.source}</span><span class="faint">{e.time?.replace('T', ' ')}</span></div>
            <div class="small muted">{MODE[e.mode] || e.mode} · {e.format || '—'}{e.ms ? ` · ${(e.ms / 1000).toFixed(1)} s` : ''}{e.backup ? ` · original backed up to ${e.backup}` : ''} · {markerText(e.marker)}</div>
            {#if e.error}<div class="small err">{e.error}</div>{/if}
            {#each e.notes || [] as n}<div class="small faint">{n}</div>{/each}
          </div>
        {/each}
      </div>
    {:else}
      <p><b>Photoband {app.version}</b> — Polaroid-style caption bands for photos. Everything runs on this computer; nothing is sent anywhere.</p>
      <p class="small muted">Released under the MIT License. Bundled components keep their own licenses.</p>
      <p class="small muted">Metadata by ExifTool (Phil Harvey). Reading text on scans (OCR): {app.ocrEngines.join(', ') || 'none available'}.</p>
      <h4>Font licenses</h4>
      {#each lic as l}
        <details><summary>{l.family} — {licenseName[String(l.license).toUpperCase()] || l.license}</summary><pre>{l.text}</pre></details>
      {/each}
    {/if}
  </div>
  {#snippet footer()}
    <button class="btn primary" onclick={() => (dialogs.helpOpen = null)}>Done</button>
  {/snippet}
</Modal>

<style>
  .keys { width: 100%; border-collapse: collapse; }
  .keys td { padding: 6px 4px; border-bottom: 1px solid var(--line); vertical-align: middle; }
  .keys td.combo { width: 1%; white-space: nowrap; padding-right: 18px; }
  .keys .grp th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--fg-3); font-weight: 600; padding: 14px 4px 4px; border-bottom: 1px solid var(--line); }
  .keys tbody:first-child .grp th { padding-top: 0; }
  .keys kbd { font-family: var(--font-ui, inherit); font-size: 11.5px; min-width: 1.6em; text-align: center; display: inline-block; }
  .keyset { display: inline-flex; align-items: center; gap: 2px; }
  .plus { color: var(--fg-3); font-size: 11px; }
  .or { color: var(--fg-3); font-size: 11px; margin: 0 6px; }
  .log { display: flex; flex-direction: column; gap: 8px; }
  .entry { border: 1px solid var(--line); border-radius: 7px; padding: 8px 10px; }
  .entry.bad { border-color: color-mix(in srgb, var(--danger) 45%, var(--line)); }
  .path { font: 11.5px var(--font-mono); word-break: break-all; }
  .small { font-size: 12px; }
  .err { color: var(--danger); }
  pre { white-space: pre-wrap; font-size: 11px; max-height: 240px; overflow: auto; background: var(--bg-3); padding: 8px; border-radius: 6px; }
  details { margin: 4px 0; }
  summary { cursor: pointer; }
  h4 { margin: 18px 0 6px; }
  .tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: -6px 0 16px; }
  .tabs button { border: 0; background: none; padding: 0 12px; min-height: 32px; color: var(--fg-2); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; font-weight: 500; }
  .tabs button:hover { color: var(--fg); }
  .tabs button.on { color: var(--fg); border-bottom-color: var(--accent); font-weight: 600; }
  .tabs button:focus-visible { outline: none; box-shadow: var(--focus); border-radius: 4px 4px 0 0; }
  .intro { margin: 0 0 12px; color: var(--fg-2); }
  .start { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
  .start li { display: flex; align-items: flex-start; gap: 14px; padding: 12px 14px; border: 1px solid var(--line); border-radius: 10px; background: var(--bg); }
  .sicon { width: 40px; height: 40px; border-radius: 10px; display: grid; place-items: center; flex: none; background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent-link); }
  .stitle { font-weight: 600; margin-bottom: 2px; }
  .snum { color: var(--fg-3); font-weight: 600; margin-right: 2px; }
  .stext { color: var(--fg-2); line-height: 1.45; margin-bottom: 6px; }
  .skey { flex: none; padding-top: 2px; }
  .tips { display: flex; flex-direction: column; gap: 6px; margin-top: 16px; color: var(--fg-2); font-size: 12.5px; }
  .tip { display: flex; align-items: center; gap: 8px; }
  .tip kbd, .start kbd { font-family: var(--font-ui, inherit); font-size: 11.5px; }
</style>
