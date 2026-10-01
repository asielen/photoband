<script lang="ts">
  import { fromDisplay, round, toDisplay, type DisplayUnit } from '../lib/units'
  import type { ScaleMode } from '../lib/types'
  let {
    value, onchange, kind = 'plain', unit = '', mode = 'relative', displayUnit = '%', photoW = 1000, dpi = null,
    min = -Infinity, max = Infinity, step = 0.1, label = '', a11yLabel = '', changed = false, onreset, disabled = false, width = 74, tip = '',
  }: {
    value: number; onchange: (v: number) => void; kind?: 'len' | 'font' | 'plain'; unit?: string; mode?: ScaleMode;
    displayUnit?: DisplayUnit; photoW?: number; dpi?: number | null; min?: number; max?: number; step?: number;
    label?: string; /** accessible name when there is no visible label */ a11yLabel?: string; changed?: boolean; onreset?: () => void; disabled?: boolean; width?: number
    /** tooltip; by default the name and what the unit means */ tip?: string
  } = $props()

  const shown = $derived(kind === 'plain' ? value : toDisplay(value, kind, mode, displayUnit, photoW, dpi))
  const suffix = $derived(kind === 'plain' ? unit : displayUnit)
  const name = $derived(label || a11yLabel)
  const UNIT_TEXT: Record<string, string> = {
    '%': 'as a % of the photo width', px: 'in pixels', pt: 'in points (1/72 in)', mm: 'in millimetres',
  }
  const unitText = $derived(kind === 'plain' ? '' : UNIT_TEXT[suffix] || '')
  // the tip says what the field is and what its unit means; the unit choice lives in Settings
  const tipText = $derived.by(() => {
    if (tip) return unitText ? `${tip.replace(/\.$/, '')}. Shown ${unitText}.` : tip
    if (!name) return undefined
    return unitText ? `${name}, ${unitText}` : name
  })
  let text = $state('')
  let editing = $state(false)
  $effect(() => {
    if (!editing) text = String(round(shown, suffix === 'px' ? 0 : 2))
  })
  function commit() {
    editing = false
    const v = parseFloat(text.replace(',', '.'))
    if (!isFinite(v)) {
      text = String(round(shown, 2))
      return
    }
    const nat = kind === 'plain' ? v : fromDisplay(v, kind, mode, displayUnit, photoW, dpi)
    const clamped = Math.min(max, Math.max(min, nat))
    if (Math.abs(clamped - value) > 1e-9) onchange(clamped)
    else text = String(round(shown, 2))
  }
  function key(e: KeyboardEvent) {
    if (e.key === 'Enter') { commit(); (e.target as HTMLInputElement).select() }
    if (e.key === 'Escape') { editing = false; text = String(round(shown, 2)); (e.target as HTMLInputElement).blur() }
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      e.preventDefault()
      const k = (e.shiftKey ? 10 : 1) * (e.key === 'ArrowUp' ? 1 : -1)
      const nat = Math.min(max, Math.max(min, value + k * step))
      onchange(+nat.toFixed(4))
    }
  }
</script>

<label class="nf" class:disabled data-tip={tipText}>
  {#if label}<span class="lbl">{label}</span>{/if}
  <span class="box" style="width:{width}px">
    <input class="field" inputmode="decimal" {disabled} bind:value={text} onfocus={(e) => { editing = true; (e.target as HTMLInputElement).select() }} onblur={commit} onkeydown={key} aria-label={name || undefined} />
    <span class="unit">{suffix}</span>
  </span>
  {#if changed}<button class="dot" data-tip="Changed for this photo. Click to use the template's value." aria-label={name ? `Reset ${name} to the template` : 'Reset to the template'} onclick={(e) => { e.preventDefault(); onreset?.() }}></button>{/if}
</label>

<style>
  .nf { display: inline-flex; align-items: center; gap: 6px; position: relative; min-width: 0; max-width: 100%; }
  .nf.disabled { opacity: 0.5; }
  .lbl { color: var(--fg-2); font-size: 12px; min-width: 0; }
  /* the inline width is the preferred size; in a tight column the box shrinks rather than overflowing */
  .box { position: relative; display: inline-block; flex: 0 1 auto; min-width: 52px; }
  .box input { width: 100%; padding-right: 26px; text-align: right; font-variant-numeric: tabular-nums; }
  .unit { position: absolute; right: 7px; top: 50%; transform: translateY(-50%); color: var(--fg-3); font-size: 11px; pointer-events: none; }
  /* 7 px dot, 16 px hit area */
  .dot { width: 16px; height: 16px; border-radius: 50%; background: transparent; border: 0; padding: 0; cursor: pointer; flex: none; display: grid; place-items: center; margin: 0 -4px; }
  .dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-link); transition: transform var(--t-fast) ease; }
  .dot:hover::before { transform: scale(1.3); }
</style>
