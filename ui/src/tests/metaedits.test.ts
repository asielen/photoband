// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { addKeywords, advancedOf, cleanText, dateBoxes, datePattern, dateState, editFromBoxes, guessedPart, isFileDate, normDate, parseAdvanced, patternParts, sameDate, detailProblem, editCount, editsSince, effectiveFaces, isDateMarker, isMarkerKeyword, peopleKeywords, rematchFaces, splitKeywords, suggestNames } from '../lib/metaedits'

describe('date editor state', () => {
  it('takes the level from photokin’s certainty keyword, else from how much is written', () => {
    const D = (iso: string, level: string, estimate = false, pattern?: string, stored?: string) =>
      ({ kind: 'date', iso, level, estimate, ...(pattern ? { pattern } : {}), ...(stored ? { stored } : {}) })
    expect(dateState({ date: '1952:06:14 00:00:00' })).toEqual(D('1952-06-14', 'day'))
    expect(dateState({ date: '1952:06' })).toEqual(D('1952-06', 'month'))
    expect(dateState({ date: '1952:06:00 00:00:00' })).toEqual(D('1952-06', 'month'))
    expect(dateState({ date: '1925:06:15 00:00:00', date_certainty: 'Y~' })).toEqual(D('1925', 'year', true, undefined, '1925-06-15'))
    expect(dateState({ date: '1944:07:15 00:00:00', date_certainty: 'Y!M~' })).toEqual(D('1944-07', 'month', true, undefined, '1944-07-15'))   // the summer
    expect(dateState({ date: '1944:11:23 00:00:00', date_certainty: 'Y!M!D~' })).toEqual(D('1944-11-23', 'day', true))  // around Thanksgiving
    expect(dateState({ date: '1960:05:15', date_certainty: 'Y!M!' })).toEqual(D('1960-05', 'month', false, undefined, '1960-05-15'))
    expect(dateState({ date: '1960:05:15', date_certainty: 'Y!M?' })).toEqual(D('1960', 'year', false, 'Y!M?', '1960-05-15'))
    expect(dateState({ date: '2017-04-05T17:01:07+02:00' })).toEqual(D('2017-04-05', 'day'))
    // the rest of photokin's spec, read as the backend reads it
    expect(dateState({ date: '1944:06:14', date_certainty: 'Y~M!D!' })).toEqual(D('1944-06-14', 'day', true, 'Y~M!D!'))   // the year is the guess: kept
    expect(dateState({ date: '1944:06:14', date_certainty: 'Y!M?D!' })).toEqual(D('1944', 'year', false, 'Y!M?D!', '1944-06-14'))
    expect(dateState({ date: '1944:06:14', date_certainty: 'Y@' })).toEqual(D('1944', 'year', true, 'Y@', '1944-06-14'))
    expect(dateState({ date: '1944:11:23', date_certainty: 'Y!M!D?' })).toEqual(D('1944-11', 'month', false, 'Y!M!D?', '1944-11-23'))
    // a birthday, the year unknown: a date now (its year a placeholder), not words
    expect(dateState({ date: '1944:06:14', date_certainty: 'Y?M!D!' })).toEqual(D('1944-06-14', 'day', false, 'Y?M!D!'))
    expect(dateState({ date: '1944:06:14', date_certainty: 'Y?' })).toEqual({ kind: 'none' })
  })
  it('the boxes leave out what the keyword says is unknown', () => {
    expect(dateBoxes(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y?M!D!' }))).toEqual({ year: null, month: 6, day: 14 })
    expect(dateBoxes(normDate({ iso: '1944-11', level: 'month', pattern: 'Y!M!D?' }))).toEqual({ year: 1944, month: 11, day: null })
    expect(dateBoxes(normDate({ iso: '1925', level: 'year', estimate: true }))).toEqual({ year: 1925, month: null, day: null })
    // a whole date typed under Advanced with a coarser keyword: the boxes agree with what prints
    expect(dateBoxes(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y!M!D?' }))).toEqual({ year: 1944, month: 6, day: null })
    expect(dateBoxes(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y!' }))).toEqual({ year: 1944, month: null, day: null })
  })
  it('which parts are the guess', () => {
    expect(guessedPart(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y~M!D!' }))).toBe('the year')
    expect(guessedPart(normDate({ iso: '1944-11-23', level: 'day', estimate: true }))).toBe('the day')
    expect(guessedPart(normDate({ iso: '1944-07', level: 'month', estimate: true }))).toBe('the month')
    expect(guessedPart(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y~M!D~' }))).toBe('the year and day')
    expect(guessedPart(normDate({ iso: '1944-06-14', level: 'day' }))).toBe('')
    expect(sameDate({ iso: '1944', level: 'circa' }, { iso: '1944', level: 'year', estimate: true, pattern: 'Y~' })).toBe(true)
    expect(sameDate({ iso: '1944-06-14', level: 'day', estimate: true }, { iso: '1944-06-14', level: 'day', estimate: true, pattern: 'Y~M!D!' })).toBe(false)
  })
  it('normalises as the backend does: the pattern says what is a guess', () => {
    expect(normDate({ iso: '1925', level: 'circa' })).toEqual({ iso: '1925', level: 'year', estimate: true })
    // as the backend: circa is an estimated year, and a pattern still says what is a guess
    expect(normDate({ iso: '1925', level: 'circa', pattern: 'Y!' })).toEqual({ iso: '1925', level: 'year', estimate: false })
    expect(['day', 'month', 'year'].map((l) => datePattern(l as any, true))).toEqual(['Y!M!D~', 'Y!M~', 'Y~'])
    expect(['day', 'month', 'year'].map((l) => datePattern(l as any, false))).toEqual(['Y!M!D!', 'Y!M!', 'Y!'])
    // the same table as tests/test_metaedit.py::test_a_date_keeps_any_pattern_that_describes_it
    expect(normDate({ iso: '1944-11', level: 'month', pattern: 'Y!M!D?' })).toEqual({ iso: '1944-11', level: 'month', estimate: false, pattern: 'Y!M!D?' })
    expect(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y?M!D!' })).toEqual({ iso: '1944-06-14', level: 'day', estimate: false, pattern: 'Y?M!D!' })
    expect(normDate({ iso: '1944-06', level: 'month', pattern: 'Y?M~' })).toEqual({ iso: '1944-06', level: 'month', estimate: true, pattern: 'Y?M~' })
    expect(normDate({ iso: '1944', level: 'year', pattern: 'Y!M?D!' })).toEqual({ iso: '1944', level: 'year', estimate: false, pattern: 'Y!M?D!' })
    expect(normDate({ iso: '1944-06-14', level: 'day', estimate: false, pattern: 'Y~M!D!' })).toEqual({ iso: '1944-06-14', level: 'day', estimate: true, pattern: 'Y~M!D!' })
    expect(normDate({ iso: '1944-06', level: 'month', estimate: false, pattern: 'Y!M@' })).toEqual({ iso: '1944-06', level: 'month', estimate: true, pattern: 'Y!M@' })
    expect(normDate({ iso: '1944-06', level: 'month', estimate: true, pattern: 'Y!M~' })).toEqual({ iso: '1944-06', level: 'month', estimate: true })
    // a pattern keeping more than the date has gives way to the level's own
    expect(normDate({ iso: '1944', level: 'year', pattern: 'Y!M!' })).toEqual({ iso: '1944', level: 'year', estimate: false })
    expect(patternParts('Y?', 'year')).toBeNull()
  })
  it('a date in words is shown as written, with its year when there is one', () => {
    expect(dateState({ date: '1950s' })).toEqual({ kind: 'text', text: '1950s', year: 1950 })
    expect(dateState({ date: 'Summer 1962' })).toEqual({ kind: 'text', text: 'Summer 1962', year: 1962 })
    expect(dateState({ date: '0000:00:00 00:00:00' })).toEqual({ kind: 'none' })
    expect(dateState({})).toEqual({ kind: 'none' })
  })
  it('the boxes make a date: blank is unknown, Estimated is the last part filled', () => {
    const E = (y: number | null, m: number | null, d: number | null, est = false, keep?: any) => editFromBoxes(y, m, d, est, keep)
    expect(E(1944, 11, 23)).toEqual({ edit: { iso: '1944-11-23', level: 'day', estimate: false } })
    expect(E(1944, 11, 23, true)).toEqual({ edit: { iso: '1944-11-23', level: 'day', estimate: true } })
    expect(E(1944, 7, null, true)).toEqual({ edit: { iso: '1944-07', level: 'month', estimate: true } })
    expect(E(1925, null, null, true)).toEqual({ edit: { iso: '1925', level: 'year', estimate: true } })
    expect(E(null, 6, 14)).toEqual({ edit: { iso: '1900-06-14', level: 'day', estimate: false, pattern: 'Y?M!D!' } })
    expect(E(null, 6, null, true)).toEqual({ edit: { iso: '1900-06', level: 'month', estimate: true, pattern: 'Y?M~' } })
    expect(E(null, 2, 29)).toEqual({ edit: { iso: '1904-02-29', level: 'day', estimate: false, pattern: 'Y?M!D!' } })
    expect(E(null, null, null)).toEqual({ edit: null })
    expect(E(1944, null, 23)).toEqual({ problem: 'A day needs its month.' })
    expect(E(194, null, null)).toHaveProperty('problem')
    expect(E(new Date().getFullYear() + 2, null, null)).toHaveProperty('problem')
    expect(E(1951, 2, 29)).toEqual({ problem: 'February has 28 days.' })
  })
  it('the boxes keep the date’s own pattern and placeholder year while the same parts are filled', () => {
    const bday = normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y~M!D!' })
    expect(editFromBoxes(1945, 6, 14, true, bday)).toEqual({ edit: { iso: '1945-06-14', level: 'day', estimate: true, pattern: 'Y~M!D!' } })
    // Estimated toggled, or another part filled: a new statement, the boxes' own pattern
    expect(editFromBoxes(1944, 6, 14, false, bday)).toEqual({ edit: { iso: '1944-06-14', level: 'day', estimate: false } })
    expect(editFromBoxes(1944, 6, null, true, bday)).toEqual({ edit: { iso: '1944-06', level: 'month', estimate: true } })
    // a year made unknown keeps the year it had as the placeholder
    const day = normDate({ iso: '1944-06-14', level: 'day' })
    expect(editFromBoxes(null, 6, 14, false, day)).toEqual({ edit: { iso: '1944-06-14', level: 'day', estimate: false, pattern: 'Y?M!D!' } })
    const unk = normDate({ iso: '1944-11', level: 'month', pattern: 'Y!M!D?' })
    expect(editFromBoxes(1944, 12, null, false, unk)).toEqual({ edit: { iso: '1944-12', level: 'month', estimate: false, pattern: 'Y!M!D?' } })
  })
  it('Advanced takes the stored date and keyword as typed', () => {
    expect(parseAdvanced('1944-11-23', 'DATE: Y!M!D?')).toEqual({ edit: { iso: '1944-11-23', level: 'day', estimate: false, pattern: 'Y!M!D?' } })
    expect(parseAdvanced('1944-06-14', 'y~m!d!')).toEqual({ edit: { iso: '1944-06-14', level: 'day', estimate: true, pattern: 'Y~M!D!' } })
    expect(parseAdvanced('1944-07', '')).toEqual({ edit: { iso: '1944-07', level: 'month', estimate: false } })
    expect(parseAdvanced('', '')).toEqual({ edit: null })
    expect(parseAdvanced('1944-02-30', 'DATE: Y!')).toMatchObject({ field: 'stored' })
    expect(parseAdvanced('44', 'DATE: Y!')).toMatchObject({ field: 'stored' })
    expect(parseAdvanced('1944-06-14', 'DATE: Y!M~D!x')).toMatchObject({ field: 'keyword' })
    expect(parseAdvanced('1944', 'DATE: Y!M!')).toMatchObject({ field: 'keyword' })
    expect(advancedOf(normDate({ iso: '1944-07', level: 'month', estimate: true }), '1944-07-15')).toEqual({ stored: '1944-07-15', keyword: 'DATE: Y!M~' })
    expect(advancedOf(normDate({ iso: '1944-06-14', level: 'day', pattern: 'Y?M!D!' }))).toEqual({ stored: '1944-06-14', keyword: 'DATE: Y?M!D!' })
  })
  it('the file’s own date, as shown or as stored, is no edit', () => {
    const f = { date: '1944:11:23 00:00:00', date_certainty: 'Y!M!D?' }
    expect(isFileDate({ iso: '1944-11', level: 'month', pattern: 'Y!M!D?' }, f)).toBe(true)
    expect(isFileDate({ iso: '1944-11-23', level: 'day', pattern: 'Y!M!D?' }, f)).toBe(true)
    expect(isFileDate({ iso: '1944-11-24', level: 'day', pattern: 'Y!M!D?' }, f)).toBe(false)
    expect(isFileDate({ iso: '1944-11', level: 'month' }, f)).toBe(false)
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
    expect(editFromBoxes(1952.5, null, null, false)).toHaveProperty('problem')
    expect(editFromBoxes(1952, 6, 1.5, false)).toHaveProperty('problem')
    expect(editFromBoxes(1952, 6.5, null, false)).toHaveProperty('problem')
  })
  it('only well-formed markers are photokin’s', () => {
    expect(isDateMarker('DATE: Y!M~')).toBe(true)
    expect(isDateMarker('Date: ask Ann')).toBe(false)
    expect(isMarkerKeyword('Date: ask Ann')).toBe(true)     // photokin takes it for its marker (kept, never printed)
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
