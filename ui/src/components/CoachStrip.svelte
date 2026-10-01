<script lang="ts">
  // A one-time hint the first time a photo opens: no modal tour, just three facts and a dismiss.
  // The dismissal is remembered per viewer (localStorage).
  import { loadFlag, saveFlag } from '../lib/prefs'
  import { shortcutLabel } from '../lib/tooltip'
  import Icon from './Icon.svelte'

  const KEY = 'coachDismissed'
  let show = $state(!loadFlag(KEY))

  function dismiss() {
    show = false
    saveFlag(KEY, true)
  }
</script>

{#if show}
  <div class="coach row" role="note" aria-label="Getting started">
    <Icon name="info" size={15} />
    <p class="grow">
      <span>Edit the caption <span class="wide">on the right, or click it in the preview</span><span class="narrow">at right</span></span>
      <span><b>Save copy</b> keeps your original</span>
      <span><kbd>{shortcutLabel('Mod+K')}</kbd> for every command</span>
    </p>
    <button class="btn sm ghost" data-tip="Hide this hint. It won't come back." onclick={dismiss}>Got it</button>
  </div>
{/if}

<style>
  .coach { gap: 12px; padding: 6px 8px 6px 12px; background: var(--info-bg); color: var(--info-fg); border-bottom: 1px solid color-mix(in srgb, var(--info-fg) 14%, transparent); font-size: 12.5px; animation: in var(--t-med) ease-out; }
  @keyframes in { from { opacity: 0; } }
  p { margin: 0; display: flex; flex-wrap: wrap; column-gap: 20px; row-gap: 2px; align-items: center; }
  p > span { white-space: nowrap; }
  .narrow { display: none; }
  @media (max-width: 1279px) {
    .wide { display: none; }
    .narrow { display: inline; }
    .coach { font-size: 12px; gap: 8px; }
    p { column-gap: 14px; }
  }
  kbd { background: transparent; color: inherit; border-color: color-mix(in srgb, currentColor 35%, transparent); }
  .coach :global(.btn) { color: inherit; flex: none; }
</style>
