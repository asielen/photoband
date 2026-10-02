/** Source pixels the edge loupe shows (at 4x, in 160 CSS px). */
export const LOUPE_SRC = 40

/** The loupe's source window around (sx, sy), always inside the W x H image: the crop it asks
 *  for is never empty (the pointer may be past the image edge while an edge is dragged) and
 *  never cut short (a part-outside crop would come back smaller and be stretched). */
export function loupeWindow(sx: number, sy: number, W: number, H: number, size = LOUPE_SRC) {
  const w = Math.max(1, Math.min(size, W))
  const h = Math.max(1, Math.min(size, H))
  const x = Math.max(0, Math.min(W - w, Math.round(sx - size / 2)))
  const y = Math.max(0, Math.min(H - h, Math.round(sy - size / 2)))
  return { x, y, w, h }
}
