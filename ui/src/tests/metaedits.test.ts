// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { addKeywords, dateState, editCount, effectiveFaces, isMarkerKeyword, isoFor, peopleKeywords, splitKeywords, suggestNames } from '../lib/metaedits'

describe('date editor state', () => {
  it('takes the level from photokin’s certainty keyword, else from how much is written', () => {
    expect(dateState({ date: '1952:06:14 00:00:00' })).toEqual({ kind: 'date', iso: '1952-06-14', level: 'day' })
    expect(dateState({ date: '1952:06' })).toEqual({ kind: 'date', iso: '1952-06', level: 'month' })
    expect(dateState({ date: '1952:06:00 00:00:00' })).toEqual({ kind: 'date', iso: '1952-06', level: 'month' })
    expect(dateState({ date: '1925:06:15 00:00:00', date_certainty: 'Y~' })).toEqual({ kind: 'date', iso: '1925', level: 'circa' })
    expect(dateState({ date: '1960:05:15 00:00:00', date_certainty: 'Y!M~' })).toEqual({ kind: 'date', iso: '1960', level: 'year' })
    expect(dateState({ date: '1960:05:15', date_certainty: 'Y!M!' })).toEqual({ kind: 'date', iso: '1960-05', level: 'month' })
    expect(dateState({ date: '1950:06:15', date_certainty: 'Y?M!D!' })).toEqual({ kind: 'none' })
    expect(dateState({ date: '2017-04-05T17:01:07+02:00' })).toEqual({ kind: 'date', iso: '2017-04-05', level: 'day' })
  })
  it('a date in words is shown as written, with its year when there is one', () => {
    expect(dateState({ date: '1950s' })).toEqual({ kind: 'text', text: '1950s', year: 1950 })
    expect(dateState({ date: 'Summer 1962' })).toEqual({ kind: 'text', text: 'Summer 1962', year: 1962 })
    expect(dateState({ date: '0000:00:00 00:00:00' })).toEqual({ kind: 'none' })
    expect(dateState({})).toEqual({ kind: 'none' })
  })
  it('only whole dates of a level are made', () => {
    expect(isoFor('day', 1952, 6, 14)).toBe('1952-06-14')
    expect(isoFor('day', 1952, 2, 30)).toBeNull()
    expect(isoFor('day', 1952, 6, null)).toBeNull()
    expect(isoFor('month', 1952, 6, null)).toBe('1952-06')
    expect(isoFor('circa', 1925, null, null)).toBe('1925')
    expect(isoFor('year', 999, null, null)).toBeNull()
    expect(isoFor('year', new Date().getFullYear() + 2, null, null)).toBeNull()
  })
})

describe('keywords', () => {
  it('splits typed and pasted lists, keeps no repeats and no date markers', () => {
    expect(splitKeywords('a, b;c\nd ,,')).toEqual(['a', 'b', 'c', 'd'])
    expect(addKeywords(['Family'], ['family', 'DATE: Y~', 'Picnic'])).toEqual(['Family', 'Picnic'])
  })
  it('knows photokin’s markers', () => {
    for (const k of ['DATE: Y~', 'Gemini Analyzed', 'back', 'Negative']) expect(isMarkerKeyword(k)).toBe(true)
    for (const k of ['Backyard', 'family']) expect(isMarkerKeyword(k)).toBe(false)
  })
  it('people keywords follow renames only in files that list people as keywords', () => {
    expect(peopleKeywords(['Ann', 'picnic'], ['Ann'], ['Anne'], 'Ann', 'Anne')).toEqual(['picnic', 'Anne'])
    expect(peopleKeywords(['picnic'], ['Ann'], ['Anne'], 'Ann', 'Anne')).toBeNull()
    // another face is still Ann: her keyword stays
    expect(peopleKeywords(['Ann', 'Bob'], ['Ann', 'Ann', 'Bob'], ['Ann', 'Anne', 'Bob'], 'Ann', 'Anne')).toEqual(['Ann', 'Bob', 'Anne'])
  })
})

describe('faces', () => {
  const meta: any = { faces: {
    named: [{ name: 'Ann', box: [0.1, 0.1, 0.1, 0.1], source: 'MWG', key: 'mwg:0' }],
    unnamed: [{ name: '', box: [0.5, 0.1, 0.1, 0.1], source: 'MWG', key: 'mwg:1' }] } }
  it('applies edits as the backend does', () => {
    const f = effectiveFaces(meta, { faces: { 'mwg:1': { name: 'Bob' }, 'mwg:0': { deleted: true }, 'new:1': { name: '', box: [0.2, 0.2, 0.1, 0.1] } } })
    expect(f.named.map((x) => x.name)).toEqual(['Bob'])
    expect(f.unnamed.map((x) => x.key)).toEqual(['new:1'])
    expect(effectiveFaces(meta, undefined).named).toBe(meta.faces.named)
  })
  it('counts edits', () => {
    expect(editCount({ title: '', faces: { a: {}, b: {} } })).toBe(3)
    expect(editCount(undefined)).toBe(0)
  })
})

describe('name suggestions', () => {
  it('match any word, recent names first, no repeats', () => {
    const pools = [['Ann Smith', 'Bob Jones'], ['ann smith', 'Annabel Lee', 'Dan Annis']]
    expect(suggestNames('an', pools)).toEqual(['Ann Smith', 'Annabel Lee', 'Dan Annis'])
    expect(suggestNames('jon', pools)).toEqual(['Bob Jones'])
    expect(suggestNames('Ann Smith', pools)).toEqual([])
  })
})
