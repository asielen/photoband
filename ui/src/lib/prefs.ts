// Small per-viewer UI preferences kept in localStorage (disclosure state, first-run hint).
// Storage can be unavailable (private window, blocked site data): every access is guarded and
// the UI works the same without it, just without remembering.

const PREFIX = 'photoband.'

export function loadPref(key: string): string | null {
  try {
    return localStorage.getItem(PREFIX + key)
  } catch {
    return null
  }
}

export function savePref(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(PREFIX + key)
    else localStorage.setItem(PREFIX + key, value)
  } catch {
    /* storage unavailable: the choice lasts for this session only */
  }
}

export function loadFlag(key: string, fallback = false): boolean {
  const v = loadPref(key)
  return v === null ? fallback : v === '1'
}

export function saveFlag(key: string, on: boolean) {
  savePref(key, on ? '1' : '0')
}
