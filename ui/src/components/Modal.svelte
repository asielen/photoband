<script module lang="ts">
  // Modal stack: every Modal takes the next z-index when it mounts, so a dialog
  // opened from another dialog (a confirm from Settings, a file picker) is always
  // on top, whatever order the components sit in the DOM. Only the top-most
  // modal handles Esc / Tab and keeps focus inside itself.
  const stack: symbol[] = []
  const refocus = new Map<symbol, () => void>() // how each open modal puts focus back inside itself
  const zs = new Map<symbol, number>() // bounded by how many modals are open (toasts sit at 200)
  const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex], [contenteditable]'
  /** How many modals are open (Toasts moves to the top of the window while one is), and the
   *  height of the toast stack there: dialogs leave that much room above them so a toast never
   *  covers a dialog's title, close button or footer. */
  export const modals = $state({ open: 0, toastH: 0 })

  function visible(el: HTMLElement) {
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  }
  export function focusables(root: HTMLElement): HTMLElement[] {
    return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
      (x) => !x.hasAttribute('disabled') && x.tabIndex >= 0 && !x.closest('[inert]') && visible(x) && (x as HTMLInputElement).type !== 'hidden',
    )
  }
</script>

<script lang="ts">
  import { untrack, type Snippet } from 'svelte'
  import Icon from './Icon.svelte'
  let {
    title,
    width = 480,
    onclose,
    children,
    footer,
    height,
    noPad = false,
  }: { title: string; width?: number; onclose?: () => void; children: Snippet; footer?: Snippet; height?: string; noPad?: boolean } = $props()
  let dialog: HTMLDivElement
  let bodyEl: HTMLDivElement
  let footEl = $state<HTMLElement>()
  const id = Symbol('modal')
  const z = Math.max(100, ...zs.values()) + 2
  zs.set(id, z)
  const isTop = () => stack[stack.length - 1] === id
  const toastRoom = $derived(modals.toastH > 0 ? modals.toastH + 8 : 0)

  /** [autofocus] → first text field in the body → footer primary (not danger) → first footer button → the dialog. */
  function initialTarget(): HTMLElement | null {
    const vis = (els: Iterable<HTMLElement>) => [...els].find((x) => !x.hasAttribute('disabled') && visible(x)) || null
    return (
      vis(dialog.querySelectorAll<HTMLElement>('[autofocus], [data-autofocus]')) ||
      vis(bodyEl.querySelectorAll<HTMLElement>('input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=file]), select, textarea, [contenteditable="true"]')) ||
      (footEl ? vis(footEl.querySelectorAll<HTMLElement>('button.primary:not(.danger)')) : null) ||
      (footEl ? vis(footEl.querySelectorAll<HTMLElement>('button')) : null) ||
      dialog
    )
  }

  $effect(() => {
    const prev = document.activeElement as HTMLElement | null
    stack.push(id)
    untrack(() => modals.open++)
    refocus.set(id, () => {
      if (dialog && !dialog.contains(document.activeElement)) initialTarget()?.focus()
    })
    queueMicrotask(() => {
      if (!dialog || !isTop()) return
      if (dialog.contains(document.activeElement) && document.activeElement !== dialog) return
      initialTarget()?.focus()
    })
    return () => {
      const i = stack.indexOf(id)
      if (i >= 0) stack.splice(i, 1)
      untrack(() => (modals.open = Math.max(0, modals.open - 1)))
      zs.delete(id)
      refocus.delete(id)
      if (prev && prev.isConnected && prev !== document.body) prev.focus?.()
      // the opener is gone (e.g. a menu item): keep focus in the dialog underneath
      const top = stack[stack.length - 1]
      if (top) queueMicrotask(() => refocus.get(top)?.())
    }
  })

  function onKey(e: KeyboardEvent) {
    if (!isTop() || !dialog) return
    if (e.key === 'Escape') {
      if (e.defaultPrevented || !onclose) return
      e.stopPropagation()
      e.preventDefault()
      onclose()
      return
    }
    if (e.key === 'Tab') {
      if (e.defaultPrevented) return
      const els = focusables(dialog)
      if (!els.length) {
        e.preventDefault()
        dialog.focus()
        return
      }
      const first = els[0]
      const last = els[els.length - 1]
      const a = document.activeElement as HTMLElement | null
      if (!a || !dialog.contains(a) || a === dialog) {
        ;(e.shiftKey ? last : first).focus()
        e.preventDefault()
      } else if (e.shiftKey && a === first) {
        last.focus()
        e.preventDefault()
      } else if (!e.shiftKey && a === last) {
        first.focus()
        e.preventDefault()
      }
    }
  }

  function onFocusIn(e: FocusEvent) {
    if (!isTop() || !dialog) return
    const t = e.target as Node | null
    if (!t || dialog.contains(t)) return
    // toasts stay usable while a dialog is open
    if ((t as HTMLElement).closest?.('.toasts, [data-outside-modal]')) return
    initialTarget()?.focus()
  }
</script>

<svelte:window onkeydown={onKey} />
<svelte:document onfocusin={onFocusIn} />

<div class="backdrop" role="presentation" style="z-index:{z}; padding-top:{toastRoom}px">
  <div class="dialog" role="dialog" aria-modal="true" aria-label={title} tabindex="-1" bind:this={dialog} style="width:min({width}px, calc(100vw - 32px)); max-height:calc(100vh - 48px - {toastRoom}px); {height ? `height:min(${height}, calc(100vh - 48px - ${toastRoom}px));` : ''}">
    <header>
      <h2>{title}</h2>
      {#if onclose}<button class="btn ghost icon sm" aria-label="Close" data-tip="Close" data-tip-key="Esc" onclick={onclose}><Icon name="x" /></button>{/if}
    </header>
    <div class="body scroll" class:noPad bind:this={bodyEl}>{@render children()}</div>
    {#if footer}<footer bind:this={footEl}>{@render footer()}</footer>{/if}
  </div>
</div>

<style>
  .backdrop { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.38); display: grid; place-items: center; z-index: 100; animation: fade 0.12s ease-out; }
  .dialog { background: var(--bg-2); border: 1px solid var(--line); border-radius: 10px; box-shadow: var(--shadow); display: flex; flex-direction: column; max-height: calc(100vh - 48px); animation: pop 0.14s ease-out; outline: none; }
  header { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px 10px 18px; border-bottom: 1px solid var(--line); }
  h2 { font-size: 14px; margin: 0; font-weight: 600; }
  .body { padding: 16px 18px; flex: 1; min-height: 0; user-select: text; }
  .body.noPad { padding: 0; }
  footer { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px; border-top: 1px solid var(--line); background: var(--bg); border-radius: 0 0 10px 10px; }
  @keyframes fade { from { opacity: 0 } }
  @keyframes pop { from { transform: translateY(6px) scale(0.98); opacity: 0 } }
</style>
