// Promise-based dialogs (confirm/choose) and file picking with native dialogs,
// falling back to the in-app browser when the OS dialog isn't available.
import { ApiError, post } from './api'

export interface ChoiceButton {
  id: string
  label: string
  kind?: 'primary' | 'danger' | 'default'
}

export interface ConfirmState {
  title: string
  message: string
  buttons: ChoiceButton[]
  checkbox?: string
  checked?: boolean
  resolve: (r: { id: string; checked: boolean }) => void
}

export interface BrowserState {
  kind: 'open-files' | 'open-folder' | 'save-file' | 'open-font' | 'open-json'
  title: string
  initial: string
  saveName: string
  resolve: (choice: BrowserChoice | null) => void
}

/** What the in-app browser returns: one-time pick ids from /api/fs/list (the server grants
 *  access only for those), or for "save as" a folder pick id plus a plain file name. */
export interface BrowserChoice {
  picks?: string[]
  save?: { folder: string; name: string }
}

class Dialogs {
  confirmState = $state<ConfirmState | null>(null)
  browserState = $state<BrowserState | null>(null)
  settingsOpen = $state<string | null>(null) // tab name
  helpOpen = $state<string | null>(null)

  ask(title: string, message: string, buttons: ChoiceButton[], checkbox?: string): Promise<{ id: string; checked: boolean }> {
    return new Promise((resolve) => {
      this.confirmState = { title, message, buttons, checkbox, checked: false, resolve }
    })
  }

  async confirm(title: string, message: string, ok = 'OK', danger = false): Promise<boolean> {
    const r = await this.ask(title, message, [
      { id: 'cancel', label: 'Cancel' },
      { id: 'ok', label: ok, kind: danger ? 'danger' : 'primary' },
    ])
    return r.id === 'ok'
  }

  close(id: string) {
    const s = this.confirmState
    this.confirmState = null
    s?.resolve({ id, checked: !!s.checked })
  }

  /** Native dialog first; in browser mode, the in-app browser if the OS dialog can't be shown.
   *  The desktop window never falls back (the server turns the in-app browser off there), and a
   *  refused request (4xx) is never retried another way: both show the error instead. */
  async pick(kind: BrowserState['kind'], title = '', initial = '', saveName = ''): Promise<string[] | null> {
    // imported here: the store imports this module
    const { app } = await import('./store.svelte')
    const desktop = app.mode === 'desktop'
    if (kind !== 'open-json') {
      try {
        const r = await post<{ paths?: string[]; unavailable?: boolean }>('/api/dialog', { kind, title, initial, saveName })
        if (!r.unavailable) return r.paths && r.paths.length ? r.paths : null
        if (desktop) {
          app.toast('error', 'The system file dialog could not be shown. Try again, or restart Photoband.')
          return null
        }
      } catch (e: any) {
        const status = e instanceof ApiError ? e.status : 0
        if (desktop || (status >= 400 && status < 500)) {
          app.toast('error', `Couldn't show the file dialog: ${e?.message || e}`)
          return null
        }
        /* browser mode, server or network error: fall through to the in-app browser */
        console.debug('native dialog failed; using the in-app browser', e)
      }
    } else if (desktop) {
      app.toast('error', 'This file type can’t be chosen with the system file dialog.')
      return null
    }
    const choice = await new Promise<BrowserChoice | null>((resolve) => {
      this.browserState = { kind, title, initial, saveName, resolve }
    })
    if (!choice || (!choice.picks?.length && !choice.save)) return null
    const r = await post<{ paths: string[] }>('/api/fs/allow', choice)
    return r.paths?.length ? r.paths : null
  }

  closeBrowser(choice: BrowserChoice | null) {
    const s = this.browserState
    this.browserState = null
    s?.resolve(choice)
  }
}

export const dialogs = new Dialogs()
