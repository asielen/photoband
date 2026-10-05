// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { addKeywords, cleanText, dateState, detailProblem, editCount, editsSince, effectiveFaces, isDateMarker, isMarkerKeyword, isoFor, peopleKeywords, rematchFaces, splitKeywords, suggestNames } from '../lib/metaedits'

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
    expect(peopleKeywords(['Ann', 'picnic'], true, ['Anne'], 'Ann', 'Anne', true)).toEqual(['picnic', 'Anne'])
    expect(peopleKeywords(['picnic'], false, ['Anne'], 'Ann', 'Anne', true)).toBeNull()
    // another face is still Ann: her keyword stays
    expect(peopleKeywords(['Ann', 'Bob'], true, ['Ann', 'Anne', 'Bob'], 'Ann', 'Anne', true)).toEqual(['Ann', 'Bob', 'Anne'])
    // a keyword the file had on its own is never taken away
    expect(peopleKeywords(['Ann', 'Bob'], true, ['Bob'], 'Ann', '', false)).toBeNull()
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

describe('review regressions', () => {
  it('only whole numbers make a date', () => {
    expect(isoFor('year', 1952.5, null, null)).toBeNull()
    expect(isoFor('day', 1952, 6, 1.5)).toBeNull()
  })
  it('only well-formed markers are photokin’s', () => {
    expect(isDateMarker('DATE: Y!M~')).toBe(true)
    expect(isDateMarker('Date: ask Ann')).toBe(false)
    expect(isMarkerKeyword('Date: ask Ann')).toBe(false)
  })
  it('text is cleaned as the backend stores it', () => {
    expect(cleanText('a\u000bb\u2028c\u0000', true)).toBe('a\nb\nc')
    expect(cleanText('a\nb\r\nc', false)).toBe('a b c')
    expect(cleanText('x\ud800y', false)).toBe('xy')
    expect(detailProblem('base64:abc')).not.toBe('')
  })
  it('edits made while saving are kept per detail; face edits are not', () => {
    const sent = { title: 'A', faces: { 'mwg:0': { name: 'Ann' } } }
    expect(editsSince({ title: 'A', city: 'Paris', faces: { 'mwg:0': { name: 'Ann' } } }, sent)).toEqual({ meta: { city: 'Paris' }, droppedFaces: false })
    expect(editsSince({ title: 'B', faces: { 'mwg:0': { name: 'Anne' } } }, sent)).toEqual({ meta: { title: 'B' }, droppedFaces: true })
    expect(editsSince(sent, sent)).toEqual({ meta: undefined, droppedFaces: false })
    // a field reset while the save ran: back to the value from before the save
    expect(editsSince({ faces: { 'mwg:0': { name: 'Ann' } } }, sent, (k) => (k === 'title' ? 'Old' : undefined))).toEqual({ meta: { title: 'Old' }, droppedFaces: false })
    // a face edit reset while the save ran is reported too
    expect(editsSince({ title: 'A' }, sent).droppedFaces).toBe(true)
  })
  it('a stale face edit goes to the same face, not just the same key; a written new face is not added again', () => {
    const meta: any = { faces: { named: [{ name: 'Ann', box: [0.5, 0.5, 0.1, 0.1], source: 'MWG', key: 'mwg:0' }],
      unnamed: [{ name: '', box: [0.8, 0.1, 0.1, 0.1], source: 'MWG', key: 'mwg:1' }] } }
    const none = effectiveFaces({ faces: { named: [{ name: 'Ann', box: null, source: 'PersonInImage', key: 'pii:0' }], unnamed: [] } } as any, { faces: { 'pii:0': { name: '' } } })
    expect(none.named.length + none.unnamed.length).toBe(0)   // neither a name nor a place: gone
    const r = rematchFaces({
      'mwg:1': { name: 'Bob', was: { name: '', box: [0.1, 0.1, 0.1, 0.1] } },        // that face moved elsewhere: gone
      'new:a': { name: 'Ann', box: [0.5, 0.5, 0.1, 0.1] },                            // already in the file
      'new:b': { name: 'Cy', box: [0.2, 0.7, 0.1, 0.1] },
    }, meta)
    expect(Object.keys(r.faces)).toEqual(['new:b'])
    expect(r.dropped).toBe(2)
  })
})
