/** `p` relative to the folder `base` when it is inside it, else null. The folder must end at a
 *  separator in `p` ("C:/a/photos" is not inside "C:/a/photo"), and a path is not inside itself.
 *  Windows and macOS file systems compare names case-insensitively: `ignoreCase` for those. */
export function relInside(base: string, p: string, ignoreCase = true): string | null {
  const b = base.replace(/[\\/]+$/, '')
  if (!b || p.length <= b.length + 1) return null
  const head = p.slice(0, b.length)
  if (ignoreCase ? head.toLowerCase() !== b.toLowerCase() : head !== b) return null
  const sep = p.charAt(b.length)
  if (sep !== '/' && sep !== '\\') return null
  return p.slice(b.length + 1)
}

/** The folder part of a path ("" when there is none). */
export function dirOf(p: string): string {
  return p.slice(0, Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'), 0))
}

/** The last part of a path. */
export function baseName(p: string): string {
  return p.split(/[\\/]/).pop() || p
}
