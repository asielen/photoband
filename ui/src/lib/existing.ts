import type { ExistingAnalysis } from './types'

/** Below this the detected photo edge is a guess worth checking (the backend's band confidence). */
export const EDGE_UNSURE = 0.6

/** How sure the detector is WHERE the photo ends, 0..1 (1 when Photoband's own record or marker
 *  gives the edge). Not a confidence that the border holds writing: the case itself says that. */
export function edgeConfidence(ex: ExistingAnalysis | null | undefined): number {
  return ex?.edgeConfidence ?? ex?.confidence ?? 1
}
