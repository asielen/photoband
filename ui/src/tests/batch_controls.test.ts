// @vitest-environment jsdom
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { COPY_REASON, copySkipReason } from '../lib/batchplan'
import { patchLeaf, syncControl, writeControl } from '../lib/controls'
import { relInside } from '../lib/paths'

describe('controls show what is stored after a write', () => {
  function checkbox(checked: boolean) {
    const el = document.createElement('input')
    el.type = 'checkbox'
    el.checked = checked
    return el
  }
  function change(el: HTMLElement): Event {
    const e = new Event('change')
    Object.defineProperty(e, 'currentTarget', { value: el })
    return e
  }

  it('puts a checkbox back when the write fails', async () => {
    const stored = { saving: { backupOriginals: true } }
    const el = checkbox(false) // the user unticked it
    const errors: any[] = []
    const ok = await writeControl(change(el), () => Promise.reject(new Error('disk full')), () => stored.saving.backupOriginals, (e) => errors.push(e))
    expect(ok).toBe(false)
    expect(el.checked).toBe(true)
    expect(errors[0].message).toBe('disk full')
  })
  it('keeps the change when it was written, and shows a value the server corrected', async () => {
    const el = checkbox(false)
    const s = { saving: { backupOriginals: true } }
    expect(await writeControl(change(el), async () => { s.saving.backupOriginals = false }, () => s.saving.backupOriginals, () => {})).toBe(true)
    expect(el.checked).toBe(false)
    const num = document.createElement('input')
    num.type = 'number'
    num.value = '30' // typed below the minimum; the server stores 50
    await writeControl(change(num), async () => {}, () => 50, () => {})
    expect(num.value).toBe('50')
  })
  it('puts a whole radio group and a select back', () => {
    const form = document.createElement('form')
    for (const v of ['same', 'subfolder', 'fixed']) {
      const r = document.createElement('input')
      r.type = 'radio'
      r.name = 'loc'
      r.value = v
      form.appendChild(r)
    }
    document.body.appendChild(form)
    const radios = [...form.querySelectorAll('input')]
    radios[2].checked = true
    syncControl(radios[2], 'subfolder')
    expect(radios.map((r) => r.checked)).toEqual([false, true, false])
    const sel = document.createElement('select')
    for (const v of ['same', 'jpeg']) sel.appendChild(Object.assign(document.createElement('option'), { value: v, textContent: v }))
    sel.value = 'jpeg'
    syncControl(sel, 'same')
    expect(sel.value).toBe('same')
  })
  it('reads the stored value a patch writes', () => {
    expect(patchLeaf({ saving: { onExists: 'ask' } }, { saving: { onExists: 'increment' } })).toBe('ask')
    expect(patchLeaf({ general: { defaultTemplate: 'a' } }, { general: { defaultTemplate: 'b' }, session: { lastTemplate: 'b' } })).toBe('a')
  })
})

describe('relInside', () => {
  it('needs a separator right after the folder', () => {
    expect(relInside('C:\\photos', 'C:\\photos\\a.jpg')).toBe('a.jpg')
    expect(relInside('C:\\photos\\', 'C:\\photos\\sub\\a.jpg')).toBe('sub\\a.jpg')
    expect(relInside('/home/u/photo', '/home/u/photos/a.jpg')).toBeNull()
    expect(relInside('C:\\photos', 'C:\\photos')).toBeNull()
    expect(relInside('C:\\photos', 'C:\\photos\\')).toBeNull()
    expect(relInside('', '/a.jpg')).toBeNull()
    expect(relInside('C:\\Photos', 'c:\\photos\\a.jpg')).toBe('a.jpg')
    expect(relInside('/a/B', '/a/b/x.jpg', false)).toBeNull()
  })
})

describe('a later batch over the folder', () => {
  it('leaves the copies Photoband made alone, whatever the options', () => {
    expect(copySkipReason({ case: 'A', isCopy: true } as any)).toBe(COPY_REASON)
    // a photo Photoband captioned in place is re-captioned (or skipped) per the options
    expect(copySkipReason({ case: 'A', isCopy: false } as any)).toBeNull()
    expect(copySkipReason({ case: 'B' } as any)).toBeNull()
    expect(copySkipReason(null)).toBeNull()
  })
})

describe('live regions', () => {
  it('are never bound to a value that ticks (elapsed time, estimates)', () => {
    const dir = join(__dirname, '..', 'components')
    const bad: string[] = []
    for (const f of readdirSync(dir).filter((n) => n.endsWith('.svelte'))) {
      const src = readFileSync(join(dir, f), 'utf8')
      const re = /<(\w+)[^>]*aria-live=[^>]*>([\s\S]*?)<\/\1>/g
      for (let m = re.exec(src); m; m = re.exec(src)) {
        if (/\b(now|elapsed|pfLine|saveLine|progressLine|formatEta|formatElapsed)\b/.test(m[2])) bad.push(`${f}: ${m[2].trim().slice(0, 80)}`)
      }
    }
    expect(bad).toEqual([])
  })
})
