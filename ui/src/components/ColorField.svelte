<script lang="ts">
  let { value, onchange, label = '', a11yLabel = '', changed = false, onreset }: {
    value: string; onchange: (v: string) => void; label?: string; /** accessible name when there is no visible label */ a11yLabel?: string; changed?: boolean; onreset?: () => void
  } = $props()
  // a11yLabel names the colour when the visible label is generic ("Colour")
  const name = $derived(a11yLabel || label || 'Text')
  let hex = $state('')
  $effect(() => { hex = (value || '#000000').toUpperCase() })
  function commitHex() {
    let h = hex.trim()
    if (!h.startsWith('#')) h = '#' + h
    if (/^#[0-9a-f]{3}$/i.test(h)) h = '#' + h.slice(1).split('').map((c) => c + c).join('')
    if (/^#[0-9a-f]{6}$/i.test(h)) onchange(h.toLowerCase())
    else hex = value.toUpperCase()
  }
</script>

<span class="cf">
  {#if label}<span class="lbl">{label}</span>{/if}
  <input type="color" value={value} oninput={(e) => onchange((e.target as HTMLInputElement).value)} aria-label="{name} colour" data-tip="Pick the {name.toLowerCase()} colour" />
  <input class="field hex" bind:value={hex} onblur={commitHex} onkeydown={(e) => e.key === 'Enter' && commitHex()} aria-label="{name} colour, hex value" maxlength="7" data-tip="Or type a colour code, such as #FFFFFF for white" />
  {#if changed}<button class="dot" data-tip="Changed for this photo. Click to use the template's colour." aria-label="Reset {name.toLowerCase()} colour to the template" onclick={() => onreset?.()}></button>{/if}
</span>

<style>
  .cf { display: inline-flex; align-items: center; gap: 6px; }
  .lbl { color: var(--fg-2); font-size: 12px; width: 72px; flex: none; }
  input[type='color'] { width: 28px; height: 26px; padding: 2px; border: 1px solid var(--line-2); border-radius: var(--radius-sm); background: var(--bg-2); cursor: pointer; }
  .hex { width: 78px; font: 12px var(--font-mono); text-transform: uppercase; }
  /* 7 px dot, 16 px hit area */
  .dot { width: 16px; height: 16px; border-radius: 50%; background: transparent; border: 0; padding: 0; cursor: pointer; flex: none; display: grid; place-items: center; margin: 0 -4px; }
  .dot::before { content: ''; width: 7px; height: 7px; border-radius: 50%; background: var(--accent-link); transition: transform var(--t-fast) ease; }
  .dot:hover::before { transform: scale(1.3); }
</style>
