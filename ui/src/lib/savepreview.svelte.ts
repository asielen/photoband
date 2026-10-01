// The last /api/save/preview answer for the open photo: fetched by SavePreview, read by JpegNotice.

export type SavePreviewInfo = {
  copy: string
  copyExists: boolean
  copyError: string
  overwrite: string
  backup: string | null
  backupExists: boolean
  /** 'backup': saving takes the photo from the untouched original backup (no further JPEG loss) */
  pixelSource?: 'backup' | 'file'
  originalBackup?: string | null
}

export const savePreview = $state<{ path: string; pv: SavePreviewInfo | null }>({ path: '', pv: null })

const dirOf = (p: string) => p.slice(0, Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')))

/** `p` relative to the folder of `photo` when it is inside it (“_originals\scan-original.tif”). */
export function relToPhoto(photo: string, p: string): string {
  const d = dirOf(photo)
  return d && p.toLowerCase().startsWith(d.toLowerCase() + p.charAt(d.length)) ? p.slice(d.length + 1) : p
}
