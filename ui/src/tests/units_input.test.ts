import { describe, expect, it } from 'vitest'
import { parseNumberInput } from '../lib/units'

describe('number field input', () => {
  it('reads a whole number, with a decimal comma or the field unit', () => {
    expect(parseNumberInput('12')).toBe(12)
    expect(parseNumberInput(' -1,5 ')).toBe(-1.5)
    expect(parseNumberInput('.5')).toBe(0.5)
    expect(parseNumberInput('5.')).toBe(5)
    expect(parseNumberInput('12 %', '%')).toBe(12)
    expect(parseNumberInput('3mm', 'mm')).toBe(3)
    expect(parseNumberInput('1.2×', '×')).toBe(1.2)
  })
  it('refuses text that only starts with a number', () => {
    for (const t of ['12abc', '1.2.3', '1,5,6', '12 px', '', ' ', '-', '0x10', '1e3', 'Infinity', '5 5'])
      expect(parseNumberInput(t, '%')).toBeNaN()
    expect(parseNumberInput('3mm', '%')).toBeNaN()
  })
})
