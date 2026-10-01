// Caption markup: **bold**, *italic*, \* and \\ escapes, newlines.
// Shared format between the token resolver (Python) and the editor.

export interface Seg {
  text: string
  bold: boolean
  italic: boolean
}

export function parseMarkup(src: string): Seg[][] {
  // returns paragraphs (split on \n), each a list of styled segments
  const paras: Seg[][] = [[]]
  let bold = false
  let italic = false
  let buf = ''
  const flush = () => {
    if (buf) paras[paras.length - 1].push({ text: buf, bold, italic })
    buf = ''
  }
  for (let i = 0; i < src.length; i++) {
    const ch = src[i]
    if (ch === '\\' && i + 1 < src.length) {
      buf += src[i + 1]
      i++
      continue
    }
    if (ch === '\n') {
      flush()
      paras.push([])
      continue
    }
    if (ch === '*') {
      if (src[i + 1] === '*') {
        flush()
        bold = !bold
        i++
      } else {
        flush()
        italic = !italic
      }
      continue
    }
    buf += ch
  }
  flush()
  return paras
}

export function plainText(src: string): string {
  return parseMarkup(src)
    .map((p) => p.map((s) => s.text).join(''))
    .join('\n')
}

export function escapePlain(s: string): string {
  return s.replace(/\\/g, '\\\\').replace(/\*/g, '\\*')
}

/** Markup → HTML for the rich block editor. */
export function markupToHtml(src: string, lowConfidence: string[] = []): string {
  const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const low = new Set(lowConfidence.map((w) => w.toLowerCase()))
  const wrapLow = (t: string) => {
    if (!low.size) return esc(t)
    return t
      .split(/(\s+)/)
      .map((w) => (w.trim() && low.has(w.replace(/[.,;:!?]+$/, '').toLowerCase()) ? `<span class="lowconf" title="Low confidence: check this word">${esc(w)}</span>` : esc(w)))
      .join('')
  }
  return parseMarkup(src)
    .map((p) =>
      p
        .map((s) => {
          let h = wrapLow(s.text)
          if (s.italic) h = `<i>${h}</i>`
          if (s.bold) h = `<b>${h}</b>`
          return h
        })
        .join(''),
    )
    .join('<br>')
}

/** DOM of the contenteditable editor → markup. */
export function htmlToMarkup(root: Node): string {
  let out = ''
  const walk = (node: Node, bold: boolean, italic: boolean, isFirstBlock: { v: boolean }) => {
    if (node.nodeType === Node.TEXT_NODE) {
      let t = (node.textContent || '').replace(/ /g, ' ')
      t = escapePlain(t)
      if (t) out += wrap(t, bold, italic)
      return
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return
    const el = node as HTMLElement
    const tag = el.tagName
    if (tag === 'BR') {
      out += '\n'
      return
    }
    const fw = el.style?.fontWeight
    const b = bold || tag === 'B' || tag === 'STRONG' || fw === 'bold' || (!!fw && parseInt(fw) >= 600)
    const it = italic || tag === 'I' || tag === 'EM' || el.style?.fontStyle === 'italic'
    const isBlock = tag === 'DIV' || tag === 'P'
    if (isBlock && out.length && !out.endsWith('\n')) out += '\n'
    el.childNodes.forEach((c) => walk(c, b, it, isFirstBlock))
  }
  root.childNodes.forEach((c) => walk(c, false, false, { v: true }))
  return normalizeMarkup(out.replace(/\n$/, (m) => (root.textContent?.endsWith('\n') ? m : '')))
}

function wrap(t: string, bold: boolean, italic: boolean): string {
  if (italic) t = `*${t}*`
  if (bold) t = `**${t}**`
  return t
}

/** Merge adjacent identical style markers: **a****b** → **ab**, *a**b* → *ab*. */
export function normalizeMarkup(s: string): string {
  // Re-serialize through the parser so style toggles are minimal.
  const paras = parseMarkup(s)
  return paras
    .map((segs) => {
      const merged: Seg[] = []
      for (const g of segs) {
        const last = merged[merged.length - 1]
        if (last && last.bold === g.bold && last.italic === g.italic) last.text += g.text
        else merged.push({ ...g })
      }
      return merged.map((g) => wrap(escapePlain(g.text), g.bold, g.italic)).join('')
    })
    .join('\n')
}
