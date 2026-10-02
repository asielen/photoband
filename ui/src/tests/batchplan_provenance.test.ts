import { describe, expect, it } from 'vitest'
import { COPY_REASON, MAYBE_COPY_REASON, copySkipReason } from '../lib/batchplan'

// The analysis reports how a Photoband output was saved from its record, else from the hidden
// marker's payload (metadata stripped): a batch must decide the same way from either.
describe('copies whose metadata was stripped', () => {
  it('are left alone when the marker payload says copy (source marker+payload)', () => {
    expect(copySkipReason({ case: 'A', source: 'marker+payload', isCopy: true } as any)).toBe(COPY_REASON)
  })

  it('are left alone when how they were saved is unknown (marker without payload)', () => {
    expect(copySkipReason({ case: 'B', source: 'marker', isCopy: false, copyUnknown: true } as any)).toBe(MAYBE_COPY_REASON)
  })

  it('a known in-place save still follows the options', () => {
    expect(copySkipReason({ case: 'A', source: 'marker+payload', isCopy: false } as any)).toBeNull()
    expect(copySkipReason({ case: 'B', source: 'detection' } as any)).toBeNull()
  })
})
