// One right-click menu for the whole app (ContextMenu.svelte draws it), opened by any list of files.
import { post } from './api'
import { app } from './store.svelte'

export type ContextItem = { label: string; run: () => void; disabled?: boolean; sep?: boolean }

export const contextMenu = $state<{ x: number; y: number; items: ContextItem[]; label: string; returnFocus: HTMLElement | null } | null>({ x: 0, y: 0, items: [], label: '', returnFocus: null })

/** Open at the mouse, or under the focused element for the keyboard (Menu key, Shift+F10). */
export function openContextMenu(e: MouseEvent | KeyboardEvent, label: string, items: ContextItem[]) {
  e.preventDefault()
  const el = e.currentTarget as HTMLElement | null
  let x: number, y: number
  if (e instanceof MouseEvent && (e.clientX || e.clientY)) {
    x = e.clientX
    y = e.clientY
  } else {
    const r = el?.getBoundingClientRect()
    x = r ? r.left + 12 : 0
    y = r ? r.bottom - 4 : 0
  }
  Object.assign(contextMenu!, { x, y, items, label, returnFocus: el })
}

export function closeContextMenu(refocus = false) {
  const el = contextMenu!.returnFocus
  contextMenu!.items = []
  if (refocus) el?.focus()
}

/** What the file manager is called here. */
export function fileManagerName(): string {
  return app.platform === 'win32' ? 'File Explorer' : app.platform === 'darwin' ? 'Finder' : 'the file manager'
}

/** The menu for a photo file: show it in the file manager, copy its path. */
export function fileItems(path: string): ContextItem[] {
  return [
    { label: `Show in ${fileManagerName()}`, run: () => revealFile(path) },
    { label: 'Copy file path', run: () => copyPath(path) },
  ]
}

export async function revealFile(path: string) {
  try {
    await post('/api/open-folder', { which: 'path', path })
  } catch (e: any) {
    app.toast('error', `Couldn’t show the file: ${e.message}`)
  }
}

async function copyPath(path: string) {
  try {
    await navigator.clipboard.writeText(path)
    app.toast('success', 'File path copied.', undefined, 2500)
  } catch {
    app.toast('error', 'Couldn’t copy to the clipboard.')
  }
}

/** Keyboard: the Menu key or Shift+F10 opens the context menu of the focused item. */
export function isContextKey(e: KeyboardEvent): boolean {
  return e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')
}
