// Batch planning rules that must match the server (photoband/server.py batch_stage and
// photoband/save.py), kept apart from BatchView so they can be tested.
import type { PhotoDraft, Settings } from './types'

type BatchSettings = Pick<Settings['batch'], 'caseC' | 'which' | 'saveMode'>

/** What a batch does with a scan that has a physical (handwritten or printed) caption, case C.
 *  The server erases such a caption on copies only: in an overwrite batch, an "erase" job for a
 *  case C photo is turned into a copy and the original is kept, whatever Settings › Saving
 *  allows. `onCopy` says the plan should read "erase on a copy (original kept)". */
export function planCaseC(bs: BatchSettings): { action: 'skip' | 'erase'; onCopy: boolean } {
  if (bs.caseC === 'skip' || bs.which === 'uncaptioned') return { action: 'skip', onCopy: false }
  return { action: 'erase', onCopy: bs.saveMode === 'overwrite' }
}

/** A case C photo whose draft does not erase (a new band that keeps the handwriting, or a
 *  replaced band) overwrites the scan itself in an overwrite batch: the server refuses that
 *  unless Settings › Saving allows overwriting scans with a physical caption. Returns the
 *  reason to skip it, or null. */
export function caseCOverwriteRefused(mode: PhotoDraft['mode'], bs: BatchSettings, allowOverwriteHandwritten: boolean): string | null {
  if (bs.saveMode !== 'overwrite' || mode === 'erase' || allowOverwriteHandwritten) return null
  return 'caption on the scan: overwriting it is off (Settings › Saving)'
}

/** The reason a batch leaves a photo alone because it is itself a captioned copy Photoband made
 *  (copies can sit next to their originals, so a later batch over the folder lists them):
 *  captioning it again would only add a copy of a copy on every run. Whatever the options for
 *  photos Photoband captioned, and not something that needs a look. Null for anything else. */
export const COPY_REASON = 'a captioned copy Photoband made'
export function copySkipReason(ex: { case?: unknown; isCopy?: boolean } | null | undefined): string | null {
  return ex?.isCopy === true ? COPY_REASON : null
}
