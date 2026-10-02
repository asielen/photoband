// Form controls bound one way (checked={x}, value={x}) to a value the server stores.
//
// Svelte only writes to the DOM when x itself changes. When the write behind a control fails, x
// stays as it was, so the control keeps showing the user's change; when the server stores a
// cleaned value equal to the old one (a number clamped back, a trimmed name), the same happens.
// After every write the control is put back on the value that is really stored.

type Control = HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement

function isControl(el: unknown): el is Control {
  const g = globalThis as any
  return (!!g.HTMLInputElement && el instanceof g.HTMLInputElement) || (!!g.HTMLSelectElement && el instanceof g.HTMLSelectElement) ||
    (!!g.HTMLTextAreaElement && el instanceof g.HTMLTextAreaElement)
}

/** Shows `stored` in the control (a radio button: in its whole group). */
export function syncControl(el: EventTarget | null | undefined, stored: unknown) {
  if (!isControl(el)) return
  const text = stored === null || stored === undefined ? '' : String(stored)
  if (el instanceof (globalThis as any).HTMLInputElement && (el as HTMLInputElement).type === 'checkbox') {
    ;(el as HTMLInputElement).checked = !!stored
  } else if (el instanceof (globalThis as any).HTMLInputElement && (el as HTMLInputElement).type === 'radio') {
    const r = el as HTMLInputElement
    const scope: ParentNode = r.form ?? r.ownerDocument
    const group = r.name ? [...scope.querySelectorAll<HTMLInputElement>('input[type=radio]')].filter((x) => x.name === r.name) : [r]
    for (const x of group) x.checked = x.value === text
  } else {
    el.value = text
  }
}

/** Runs `write` for a control's change event. Afterwards, failed or not, the control shows
 *  `stored()` (read after the write); a failure is reported with `onError`. True when written. */
export async function writeControl(e: Event | null | undefined, write: () => Promise<unknown>, stored: () => unknown, onError: (err: any) => void): Promise<boolean> {
  // read now: currentTarget is cleared once the event has been dispatched
  const el = e ? (e.currentTarget ?? e.target) : null
  let ok = true
  try {
    await write()
  } catch (err) {
    ok = false
    onError(err)
  }
  syncControl(el, stored())
  return ok
}

/** The stored value a settings patch writes ({section: {key: value}}: the first key). */
export function patchLeaf(settings: any, patch: Record<string, any>): unknown {
  const [sec, v] = Object.entries(patch)[0] ?? []
  if (!sec || !v || typeof v !== 'object') return undefined
  const key = Object.keys(v)[0]
  return settings?.[sec]?.[key]
}
