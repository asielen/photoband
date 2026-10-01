/** Progress of a long run (checking photos, saving a batch): elapsed time and a calm estimate
 *  of the time left, from the average time per photo completed so far. */

/** Photos that must be completed before an estimate is shown. */
export const MIN_DONE = 2
/** Weight of a new estimate against the previous one (keeps the number from jumping). */
const SMOOTH = 0.35

/** "0:42", "3:05", "1:02:03" */
export function formatElapsed(sec: number): string {
  const s = Math.max(0, Math.floor(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = String(s % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`
}

/** "a few seconds left", "about 40 sec left", "about 3 min left", "about 1 h 20 min left" */
export function formatEta(sec: number): string {
  if (!Number.isFinite(sec) || sec < 10) return 'a few seconds left'
  if (sec < 55) return `about ${Math.max(10, Math.round(sec / 10) * 10)} sec left`
  if (sec < 90) return 'about 1 min left'
  const min = Math.round(sec / 60)
  if (min < 60) return `about ${min} min left`
  // hours: to the nearest 5 minutes
  const total5 = Math.round(sec / 300) * 5
  const h = Math.floor(total5 / 60)
  const m = total5 % 60
  return m ? `about ${h} h ${m} min left` : `about ${h} h left`
}

/** "42% · 12 of 30 photos · 1:05 elapsed · about 3 min left" (no estimate until it is meaningful). */
export function progressLine(done: number, total: number, elapsed: number, left: number | null): string {
  const pct = total > 0 ? Math.floor((Math.min(done, total) / total) * 100) : 0
  const parts = [`${pct}%`, `${Math.min(done, total)} of ${total} photo${total === 1 ? '' : 's'}`, `${formatElapsed(elapsed)} elapsed`]
  if (left !== null && done < total) parts.push(formatEta(left))
  return parts.join(' · ')
}

/** Tracks one run. Call `update` on every poll or tick; photos already done when it is first
 *  called (a resumed batch) don't count towards the speed. Photos may finish several at a time
 *  (parallel workers): the speed is photos completed per second of the whole run. Between
 *  completions the estimate counts down; each completion blends in a fresh estimate. */
export class ProgressClock {
  readonly start: number
  private base: number | null = null
  private last = 0
  private lastT = 0
  private etaAt: number | null = null

  constructor(now = Date.now()) {
    this.start = now
  }

  update(done: number, total: number, now = Date.now()): { elapsed: number; left: number | null } {
    const elapsed = Math.max(0, (now - this.start) / 1000)
    if (this.base === null || done < this.base) {
      // first call, or fewer done than before (failed photos queued again): count from here
      this.base = done
      this.last = done
      this.lastT = now
      this.etaAt = null
    }
    if (done > this.last) {
      const n = done - this.base
      if (n >= MIN_DONE) {
        const raw = (Math.max(0, total - done) * elapsed) / n
        const prev = this.etaAt === null ? null : Math.max(0, this.etaAt - (now - this.lastT) / 1000)
        this.etaAt = prev === null ? raw : SMOOTH * raw + (1 - SMOOTH) * prev
      }
      this.last = done
      this.lastT = now
    }
    if (done >= total) return { elapsed, left: 0 }
    return { elapsed, left: this.etaAt === null ? null : Math.max(0, this.etaAt - (now - this.lastT) / 1000) }
  }
}
