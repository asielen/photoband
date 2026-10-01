import { describe, expect, it } from 'vitest'
import { dateRowText } from '../lib/datetext'

describe('Details date rows', () => {
  it('hide a placeholder date that names no date', () => {
    for (const raw of ['0000:00:00 00:00:00', '0000:00:00', '    :  :  ', '', undefined]) {
      expect(dateRowText('', raw)).toBe('')
    }
  })
  it('show the caption form with the raw value when they differ', () => {
    expect(dateRowText('June 14, 1952', '1952:06:14 10:00:00')).toBe('June 14, 1952 (1952:06:14 10:00:00)')
  })
  it('show the raw value alone when it prints as written or has not resolved yet', () => {
    expect(dateRowText('circa 1950', 'circa 1950')).toBe('circa 1950')
    expect(dateRowText('', '1952:06:14')).toBe('1952:06:14')
  })
})
