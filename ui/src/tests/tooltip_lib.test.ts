// @vitest-environment jsdom
// Unit tests for the app-wide tooltip system (ui/src/lib/tooltip.ts).
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

type TipModule = typeof import('../lib/tooltip')

async function freshModule(platform = 'Linux x86_64'): Promise<TipModule> {
  vi.resetModules()
  Object.defineProperty(navigator, 'platform', { value: platform, configurable: true })
  return await import('../lib/tooltip')
}

describe('shortcutLabel', () => {
  it('spells Mod and Alt out on Windows and Linux', async () => {
    const { shortcutLabel } = await freshModule('Win32')
    expect(shortcutLabel('Mod+S')).toBe('Ctrl+S')
    expect(shortcutLabel('Mod+Shift+S')).toBe('Ctrl+Shift+S')
    expect(shortcutLabel('Alt+Left')).toBe('Alt+Left')
    expect(shortcutLabel('F1')).toBe('F1')
    expect(shortcutLabel('E')).toBe('E')
  })
  it('uses ⌘ and ⌥ on a Mac', async () => {
    const { shortcutLabel } = await freshModule('MacIntel')
    expect(shortcutLabel('Mod+S')).toBe('⌘S')
    expect(shortcutLabel('Mod+Shift+O')).toBe('⌘Shift+O')
    expect(shortcutLabel('Alt+Left')).toBe('⌥Left')
  })
})

describe('tooltips in the page', () => {
  let tip: TipModule
  let hit: Element | null = null
  // installTooltips adds document listeners for good: install once, reset between tests
  beforeAll(async () => {
    tip = await freshModule('Win32')
    // jsdom has no layout: hit-testing returns whatever the test points at
    ;(document as any).elementFromPoint = () => hit
    tip.installTooltips()
    // a tooltip shows at once within 600 ms of the last one (or of page start): start "cold"
    await new Promise((r) => setTimeout(r, 700))
  })
  beforeEach(() => {
    // performance.now() stays real: the "warm" window is measured with it
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame'] })
    tip.setTooltipsEnabled(true)
    tip.hide()
    document.body.innerHTML = ''
    hit = null
  })
  afterEach(() => {
    tip.hide()
    vi.advanceTimersByTime(2000) // let the "warm" window after a hide pass
    vi.useRealTimers()
  })

  const box = () => document.getElementById('pb-tip')
  const shown = () => box()?.dataset.show === '1'
  function pointAt(el: Element | null) {
    hit = el
    document.dispatchEvent(new PointerEvent('pointermove', { clientX: 10, clientY: 10, pointerType: 'mouse' } as any))
    vi.advanceTimersByTime(20) // the animation frame (fake timers run it at ~16 ms)
  }

  it('shows data-tip text after a short hover delay, with the shortcut chip', () => {
    document.body.innerHTML = '<button data-tip="Save a captioned copy." data-tip-key="Mod+S">Save</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    expect(shown()).toBe(false) // not instantly
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
    expect(box()!.querySelector('.pb-tip-text')!.textContent).toBe('Save a captioned copy.')
    expect(box()!.querySelector('kbd')!.textContent).toBe('Ctrl+S')
    expect(b.getAttribute('aria-describedby')).toBe('pb-tip')
    expect(box()!.getAttribute('role')).toBe('tooltip')
  })

  it('finds the host from a child element (an icon inside a button)', () => {
    document.body.innerHTML = '<button data-tip="Close"><svg><path></path></svg></button>'
    pointAt(document.querySelector('path'))
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
    expect(box()!.textContent).toBe('Close')
  })

  it('adopts a plain title so the native tooltip never doubles up', () => {
    document.body.innerHTML = '<button title="Old style tip">x</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    expect(b.hasAttribute('title')).toBe(false)
    expect(b.getAttribute('data-tip')).toBe('Old style tip')
    vi.advanceTimersByTime(500)
    expect(box()!.textContent).toBe('Old style tip')
  })

  it('adopts a title set later by the framework (mutation observer)', async () => {
    document.body.innerHTML = '<button>x</button>'
    const b = document.querySelector('button')!
    b.setAttribute('title', 'Set later')
    await Promise.resolve() // mutation records are delivered as a microtask
    expect(b.hasAttribute('title')).toBe(false)
    expect(b.getAttribute('data-tip')).toBe('Set later')
  })

  it('keeps an explicit data-tip over a title', () => {
    document.body.innerHTML = '<button data-tip="Explicit" title="Native">x</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    vi.advanceTimersByTime(500)
    expect(box()!.textContent).toBe('Explicit')
    expect(b.hasAttribute('title')).toBe(false)
  })

  it('shows over disabled controls, which is where it explains why', () => {
    document.body.innerHTML = '<button disabled data-tip="Open a photo first.">Save</button>'
    pointAt(document.querySelector('button'))
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
    expect(box()!.textContent).toBe('Open a photo first.')
  })

  it('data-tip-off stops the search for a host', () => {
    document.body.innerHTML = '<div data-tip="Outer"><button data-tip-off>inner</button></div>'
    pointAt(document.querySelector('button'))
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(false)
  })

  it('hides on Escape and on pointer down, and clears aria-describedby', () => {
    document.body.innerHTML = '<button data-tip="Tip" aria-describedby="other">x</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    vi.advanceTimersByTime(500)
    expect(b.getAttribute('aria-describedby')).toBe('other pb-tip')
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(shown()).toBe(false)
    expect(b.getAttribute('aria-describedby')).toBe('other')
    pointAt(null)
    vi.advanceTimersByTime(2000)
    pointAt(b)
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
    document.dispatchEvent(new PointerEvent('pointerdown'))
    expect(shown()).toBe(false)
  })

  it('moving to the next control while one is shown switches at once', () => {
    document.body.innerHTML = '<button id="a" data-tip="A">a</button><button id="b" data-tip="B">b</button>'
    pointAt(document.getElementById('a'))
    vi.advanceTimersByTime(500)
    pointAt(document.getElementById('b'))
    expect(shown()).toBe(true)
    expect(box()!.textContent).toBe('B')
  })

  it('follows a changed data-tip while shown, and hides when the host goes away', async () => {
    document.body.innerHTML = '<button data-tip="Before">x</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    vi.advanceTimersByTime(500)
    b.setAttribute('data-tip', 'After')
    await Promise.resolve()
    expect(box()!.textContent).toBe('After')
    b.remove()
    await Promise.resolve()
    expect(shown()).toBe(false)
  })

  it('setTooltipsEnabled(false) hides the current tip and shows no new ones', () => {
    document.body.innerHTML = '<button data-tip="Tip">x</button>'
    const b = document.querySelector('button')!
    pointAt(b)
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
    tip.setTooltipsEnabled(false)
    expect(shown()).toBe(false)
    pointAt(null)
    vi.advanceTimersByTime(2000)
    pointAt(b)
    vi.advanceTimersByTime(1000)
    expect(shown()).toBe(false)
    tip.setTooltipsEnabled(true)
    pointAt(null)
    vi.advanceTimersByTime(2000)
    pointAt(b)
    vi.advanceTimersByTime(500)
    expect(shown()).toBe(true)
  })

  it('shows on keyboard focus (focus-visible) but not on a mouse focus', () => {
    document.body.innerHTML = '<button data-tip="Keyboard tip">x</button>'
    const b = document.querySelector('button')! as HTMLButtonElement
    const realMatches = b.matches.bind(b)
    let keyboard = false
    b.matches = ((sel: string) => (sel === ':focus-visible' ? keyboard : realMatches(sel))) as any
    b.focus()
    vi.advanceTimersByTime(400)
    expect(shown()).toBe(false)
    b.blur()
    keyboard = true
    b.focus()
    vi.advanceTimersByTime(400)
    expect(shown()).toBe(true)
    expect(box()!.textContent).toBe('Keyboard tip')
    b.blur()
    expect(shown()).toBe(false)
  })
})

describe('tooltipsWanted (Settings › General › Show tooltips)', () => {
  it('is on by default and while settings load, off only when turned off', async () => {
    vi.resetModules()
    vi.doMock('../lib/store.svelte', () => ({ app: { settings: null } }))
    const { tooltipsWanted } = await import('../lib/tooltipprefs.svelte')
    expect(tooltipsWanted(null)).toBe(true)
    expect(tooltipsWanted({ general: {} })).toBe(true)
    expect(tooltipsWanted({ general: { showTooltips: true } })).toBe(true)
    expect(tooltipsWanted({ general: { showTooltips: false } })).toBe(false)
    vi.doUnmock('../lib/store.svelte')
  })
})
