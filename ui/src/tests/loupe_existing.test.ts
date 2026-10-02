// The edge loupe's crop window and what the existing-caption "confidence" means.
import { describe, expect, it } from 'vitest'
import { LOUPE_SRC, loupeWindow } from '../lib/loupe'
import { EDGE_UNSURE, edgeConfidence } from '../lib/existing'

// the woodbury-156 scan, upright: its top border is only 49 px, so dragging the top edge a few
// screen px up put the pointer past the image and the loupe asked for an empty crop (a 400, a
// broken image, alt text "Edge at 400%" on black)
const W = 4226
const H = 2898

describe('loupeWindow', () => {
  it('is the 40 px square around the pointer inside the image', () => {
    expect(loupeWindow(2000, 2627, W, H)).toEqual({ x: 1980, y: 2607, w: LOUPE_SRC, h: LOUPE_SRC })
  })
  it('stays a full square inside the image when the pointer is past any edge', () => {
    for (const [sx, sy] of [[-100, -100], [2000, -30], [-30, 20], [W + 500, 100], [100, H + 300], [W + 1, H + 1], [4220, 2890]]) {
      const r = loupeWindow(sx, sy, W, H)
      expect(r.w).toBe(LOUPE_SRC)
      expect(r.h).toBe(LOUPE_SRC)
      expect(r.x).toBeGreaterThanOrEqual(0)
      expect(r.y).toBeGreaterThanOrEqual(0)
      expect(r.x + r.w).toBeLessThanOrEqual(W)
      expect(r.y + r.h).toBeLessThanOrEqual(H)
    }
  })
  it('never asks for more than a tiny image has', () => {
    expect(loupeWindow(5, 5, 20, 30)).toEqual({ x: 0, y: 0, w: 20, h: 30 })
  })
})

describe('edgeConfidence', () => {
  it('is how sure the photo edge is, from edgeConfidence or the older confidence field', () => {
    expect(edgeConfidence({ case: 'C', source: 'detection', warnings: [], edgeConfidence: 0.42, confidence: 0.42 })).toBe(0.42)
    expect(edgeConfidence({ case: 'B', source: 'detection', warnings: [], confidence: 0.83 })).toBe(0.83)
    expect(edgeConfidence(null)).toBe(1)
    expect(0.55 < EDGE_UNSURE && 0.83 >= EDGE_UNSURE).toBe(true)
  })
})
