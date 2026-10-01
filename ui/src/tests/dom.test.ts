// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import { htmlToMarkup, markupToHtml } from '../lib/markup'

describe('editor round trip', () => {
  it('markup → HTML → markup is stable', () => {
    for (const m of ['plain', '**bold** and *it*', 'line1\nline2', 'star \\* and \\\\', '***both***', 'a\n\nb']) {
      const div = document.createElement('div')
      div.innerHTML = markupToHtml(m)
      expect(htmlToMarkup(div)).toBe(m.replace('***both***', '***both***'))
    }
  })
  it('handles div-wrapped lines from contenteditable', () => {
    const div = document.createElement('div')
    div.innerHTML = 'one<div>two</div><div><b>three</b></div>'
    expect(htmlToMarkup(div)).toBe('one\ntwo\n**three**')
  })
})
