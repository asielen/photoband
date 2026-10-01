<script lang="ts">
  import { dialogs } from '../lib/dialogs.svelte'
  import Modal from './Modal.svelte'
  const s = $derived(dialogs.confirmState)
  // Initial focus: never the destructive button. In a danger dialog focus the safe
  // choice (Cancel); otherwise the primary action.
  const focusIdx = $derived.by(() => {
    if (!s) return -1
    const b = s.buttons
    if (b.some((x) => x.kind === 'danger')) {
      const c = b.findIndex((x) => x.id === 'cancel')
      return c >= 0 ? c : b.findIndex((x) => x.kind !== 'danger')
    }
    const p = b.findIndex((x) => x.kind === 'primary')
    return p >= 0 ? p : b.length - 1
  })
</script>

{#if s}
  {#key s}
    <Modal title={s.title} width={440} onclose={() => dialogs.close('cancel')}>
      <p class="msg">{s.message}</p>
      {#if s.checkbox}
        <label class="row chk"><input type="checkbox" bind:checked={s.checked} /> {s.checkbox}</label>
      {/if}
      {#snippet footer()}
        {#each s.buttons as b, i}
          <button class="btn {b.kind === 'primary' ? 'primary' : b.kind === 'danger' ? 'danger solid' : ''}" data-autofocus={i === focusIdx ? '' : undefined} onclick={() => dialogs.close(b.id)}>{b.label}</button>
        {/each}
      {/snippet}
    </Modal>
  {/key}
{/if}

<style>
  .msg { margin: 0; white-space: pre-wrap; line-height: 1.5; }
  .chk { margin-top: 14px; color: var(--fg-2); }
</style>
