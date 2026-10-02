// The last /api/save/preview answer for the open photo: fetched by SavePreview, read by JpegNotice.

export type SavePreviewInfo = {
  copy: string
  copyExists: boolean
  copyError: string
  overwrite: string
  /** the exact file Overwrite keeps: an existing backup it reuses, or the new one's name */
  backup: string | null
  /** 'original': that file is the untouched original; 'current': it is this already-captioned file as it is now */
  backupKind: 'original' | 'current' | ''
  /** the untouched original is already backed up there */
  backupExists: boolean
  /** the file has a Photoband record (it was captioned in place before) */
  captioned: boolean
  /** 'backup': saving takes the photo from the untouched original backup (no further JPEG loss) */
  /** 'unverified': the original backup matches, but this file's pixels weren't checked yet */
  pixelSource?: 'backup' | 'unverified' | 'file'
  originalBackup?: string | null
}

export const savePreview = $state<{ path: string; pv: SavePreviewInfo | null }>({ path: '', pv: null })

const dirOf = (p: string) => p.slice(0, Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')))

/** `p` relative to the folder of `photo` when it is inside it (“_originals\scan-original.tif”). */
export function relToPhoto(photo: string, p: string): string {
  const d = dirOf(photo)
  // inside only when the folder is followed by a separator ("D:\Scans2" is not inside "D:\Scans")
  return d && p.toLowerCase().startsWith(d.toLowerCase()) && '/\\'.includes(p.charAt(d.length)) ? p.slice(d.length + 1) : p
}
