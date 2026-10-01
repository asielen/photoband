import { describe, expect, it } from 'vitest'
import { formatElapsed, formatEta, progressLine, ProgressClock } from '../lib/progress'

describe('formatEta', () => {
  it('rounds to calm words', () => {
    expect(formatEta(3)).toBe('a few seconds left')
    expect(formatEta(9.9)).toBe('a few seconds left')
    expect(formatEta(12)).toBe('about 10 sec left')
    expect(formatEta(44)).toBe('about 40 sec left')
    expect(formatEta(60)).toBe('about 1 min left')
    expect(formatEta(170)).toBe('about 3 min left')
    expect(formatEta(59 * 60)).toBe('about 59 min left')
    expect(formatEta(3600)).toBe('about 1 h left')
    expect(formatEta(80 * 60 + 40)).toBe('about 1 h 20 min left')
    expect(formatEta(Infinity)).toBe('a few seconds left')
  })
})

describe('formatElapsed', () => {
  it('shows minutes and seconds, hours when needed', () => {
    expect(formatElapsed(0)).toBe('0:00')
    expect(formatElapsed(42.7)).toBe('0:42')
    expect(formatElapsed(185)).toBe('3:05')
    expect(formatElapsed(3723)).toBe('1:02:03')
    expect(formatElapsed(-5)).toBe('0:00')
  })
})

describe('progressLine', () => {
  it('percent, count, elapsed and the estimate once there is one', () => {
    expect(progressLine(12, 30, 65, 170)).toBe('40% · 12 of 30 photos · 1:05 elapsed · about 3 min left')
    expect(progressLine(0, 1, 2, null)).toBe('0% · 0 of 1 photo · 0:02 elapsed')
    expect(progressLine(30, 30, 300, 0)).toBe('100% · 30 of 30 photos · 5:00 elapsed')
  })
})

describe('ProgressClock', () => {
  it('shows no estimate before two photos are done', () => {
    const c = new ProgressClock(0)
    expect(c.update(0, 10, 0).left).toBeNull()
    expect(c.update(1, 10, 10_000).left).toBeNull()
    const r = c.update(2, 10, 20_000)
    expect(r.elapsed).toBe(20)
    expect(r.left).toBeCloseTo(80) // 10 s per photo, 8 to go
  })
  it('counts down between photos and smooths a sudden change', () => {
    const c = new ProgressClock(0)
    c.update(0, 10, 0)
    c.update(2, 10, 20_000) // 80 s left
    expect(c.update(2, 10, 25_000).left).toBeCloseTo(75)
    // a quick photo: the fresh estimate (7 to go at 26/3 s each) is blended in, not taken as is
    const left = c.update(3, 10, 26_000).left!
    const raw = (7 * 26) / 3
    expect(left).toBeLessThan(74)
    expect(left).toBeGreaterThan(raw)
  })
  it('ignores photos already done when a run resumes, and restarts when the count drops', () => {
    const c = new ProgressClock(0)
    c.update(50, 100, 0)
    expect(c.update(51, 100, 5_000).left).toBeNull()
    expect(c.update(52, 100, 10_000).left).toBeCloseTo(240) // 5 s per photo, 48 to go
    c.update(40, 100, 11_000) // failed photos queued again
    expect(c.update(41, 100, 12_000).left).toBeNull()
  })
  it('handles photos finishing several at a time (parallel workers)', () => {
    const c = new ProgressClock(0)
    c.update(0, 20, 0)
    expect(c.update(4, 20, 8_000).left).toBeCloseTo(32) // 4 photos per 8 s, 16 to go
    expect(c.update(20, 20, 40_000).left).toBe(0)
  })
})
