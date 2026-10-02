// The photo's date and the scan date are separate fields in the Insert menu and autocomplete.
import { describe, expect, it } from 'vitest'
import { fieldChoices, fieldLabel, filterChoices } from '../lib/tokenlabels'

const server = [{ name: 'date' }, { name: 'digitized' }, { name: 'today' }, { name: 'title' }]

describe('date fields', () => {
  it('offers the scan date as its own plain-language field', () => {
    const choices = fieldChoices(server)
    const scan = choices.filter((c) => c.insert.startsWith('digitized'))
    expect(scan.map((c) => c.label)).toEqual(['Scan date', 'Scan year'])
    // listed once, not again as a raw server token
    expect(choices.filter((c) => c.insert === 'digitized')).toEqual([])
  })

  it('says the photo date is never the scan date', () => {
    const date = fieldChoices(server).find((c) => c.insert === 'date:mmmm d, yyyy')
    expect(date?.hint).toMatch(/never the date it was scanned/)
  })

  it('labels empty-field reports by plain name', () => {
    expect(fieldLabel('digitized')).toBe('Scan date')
    expect(fieldLabel('digitized:yyyy')).toBe('Scan date')
    expect(fieldLabel('date')).toBe('Date')
  })

  it('finds the scan date by typing "scan" or the token name', () => {
    const choices = fieldChoices(server)
    expect(filterChoices(choices, 'scan').map((c) => c.insert)).toEqual(['digitized:mmmm d, yyyy', 'digitized:yyyy'])
    expect(filterChoices(choices, 'digi').length).toBe(2)
  })
})
