// App-wide hover and focus tooltips.
//
// Any element with `data-tip="…"` (or a plain `title`, which is converted so the slow,
// unstyled native tooltip never shows) gets a themed tooltip. Optional attributes:
//   data-tip-key="Ctrl+S"   keyboard shortcut, shown as a key chip ("Mod+" becomes ⌘ / Ctrl+)
//   data-tip-side="bottom"  preferred side: top | bottom | left | right (default: below,
//                           flipped above when there is no room)
//
// Behaviour:
//   - pointer: shows after a short delay; moving between tooltipped controls within a moment
//     of the last one shows the next one immediately (as on macOS)
//   - works over disabled controls (found by hit-testing, since disabled buttons get no
//     pointer events), so "why is this greyed out" is always answerable
//   - keyboard: shows on keyboard focus (focus-visible), linked with aria-describedby
//   - hides on click, Escape, scroll, window blur, or when the element goes away
//   - can be turned off with setTooltipsEnabled(false)

const SHOW_DELAY = 450
const WARM_MS = 600 // after a tooltip hides, the next one shows at once for this long
const GAP = 8

let el: HTMLDivElement | null = null
let target: Element | null = null
let showTimer = 0
let lastHide = 0
let enabled = true
let pendingMove = 0
let lastPoint: { x: number; y: number } | null = null
const isMac = typeof navigator !== 'undefined' && /mac/i.test(navigator.platform)

export function setTooltipsEnabled(on: boolean) {
  enabled = on
  if (!on) hide()
}

export function shortcutLabel(key: string): string {
  return key.replace(/\bMod\+/g, isMac ? '⌘' : 'Ctrl+').replace(/\bAlt\+/g, isMac ? '⌥' : 'Alt+')
}

/** Find the tooltip host for a node: nearest ancestor with data-tip (or a title we adopt). */
function hostOf(node: Element | null): Element | null {
  let n: Element | null = node
  while (n && n !== document.body) {
    if (n.hasAttribute('title')) adopt(n)
    if (n.getAttribute('data-tip')) return n
    if (n.hasAttribute('data-tip-off')) return null
    n = n.parentElement
  }
  return null
}

/** Move a native title into data-tip so the browser's own tooltip doesn't double up. */
const adopted = new WeakSet<Element>()
function adopt(n: Element) {
  const t = n.getAttribute('title')
  n.removeAttribute('title')
  if (!t) return
  if (adopted.has(n) || !n.getAttribute('data-tip')) {
    n.setAttribute('data-tip', t)
    adopted.add(n)
  }
}

function ensureEl(): HTMLDivElement {
  if (el) return el
  el = document.createElement('div')
  el.className = 'pb-tip'
  el.id = 'pb-tip'
  el.setAttribute('role', 'tooltip')
  document.documentElement.appendChild(el) // outside <body>, which the observer watches
  return el
}

function render(host: Element) {
  const box = ensureEl()
  const text = host.getAttribute('data-tip') || ''
  const key = host.getAttribute('data-tip-key')
  box.textContent = ''
  const span = document.createElement('span')
  span.className = 'pb-tip-text'
  span.textContent = text
  box.appendChild(span)
  if (key) {
    const k = document.createElement('kbd')
    k.textContent = shortcutLabel(key)
    box.appendChild(k)
  }
}

function place(host: Element) {
  const box = ensureEl()
  const r = host.getBoundingClientRect()
  box.style.left = '0px'
  box.style.top = '0px'
  box.dataset.show = '1'
  const b = box.getBoundingClientRect()
  const vw = window.innerWidth, vh = window.innerHeight
  const want = (host.getAttribute('data-tip-side') || 'bottom') as 'top' | 'bottom' | 'left' | 'right'
  let x = 0, y = 0
  const fits = {
    bottom: r.bottom + GAP + b.height <= vh - 4,
    top: r.top - GAP - b.height >= 4,
    right: r.right + GAP + b.width <= vw - 4,
    left: r.left - GAP - b.width >= 4,
  }
  const order = { bottom: ['bottom', 'top'], top: ['top', 'bottom'], right: ['right', 'left', 'bottom'], left: ['left', 'right', 'bottom'] }[want]
  const side = (order.find((s) => fits[s as keyof typeof fits]) || order[0]) as keyof typeof fits
  if (side === 'bottom' || side === 'top') {
    x = r.left + r.width / 2 - b.width / 2
    y = side === 'bottom' ? r.bottom + GAP : r.top - GAP - b.height
  } else {
    y = r.top + r.height / 2 - b.height / 2
    x = side === 'right' ? r.right + GAP : r.left - GAP - b.width
  }
  x = Math.max(6, Math.min(vw - b.width - 6, x))
  y = Math.max(6, Math.min(vh - b.height - 6, y))
  box.style.left = `${Math.round(x)}px`
  box.style.top = `${Math.round(y)}px`
  box.dataset.side = side
}

function show(host: Element) {
  if (!enabled || !host.isConnected) return
  target = host
  render(host)
  place(host)
  const prev = host.getAttribute('aria-describedby') || ''
  if (!prev.split(' ').includes('pb-tip')) host.setAttribute('aria-describedby', (prev + ' pb-tip').trim())
}

export function hide() {
  clearTimeout(showTimer)
  if (target) {
    const d = (target.getAttribute('aria-describedby') || '').split(' ').filter((x) => x && x !== 'pb-tip').join(' ')
    if (d) target.setAttribute('aria-describedby', d)
    else target.removeAttribute('aria-describedby')
    lastHide = performance.now()
  }
  target = null
  if (el) delete el.dataset.show
}

function schedule(host: Element | null) {
  if (host === target) return
  clearTimeout(showTimer)
  if (!host) {
    if (target) hide()
    return
  }
  const warm = !!target || performance.now() - lastHide < WARM_MS
  if (target) hide()
  if (warm) show(host)
  else showTimer = window.setTimeout(() => show(host), SHOW_DELAY)
}

function onMove(e: PointerEvent) {
  if (e.pointerType === 'touch') return
  lastPoint = { x: e.clientX, y: e.clientY }
  if (pendingMove) return
  pendingMove = requestAnimationFrame(() => {
    pendingMove = 0
    if (!lastPoint) return
    // hit-test rather than use e.target: disabled buttons swallow pointer events
    const hit = document.elementFromPoint(lastPoint.x, lastPoint.y)
    if (hit && el && el.contains(hit)) return
    schedule(hostOf(hit))
  })
}

function onFocus(e: FocusEvent) {
  const t = e.target as Element | null
  if (!t || !(t as HTMLElement).matches?.(':focus-visible')) return
  const host = hostOf(t)
  if (host === t) {
    clearTimeout(showTimer)
    showTimer = window.setTimeout(() => show(host), 250)
  }
}

export function installTooltips() {
  if (typeof document === 'undefined') return
  document.addEventListener('pointermove', onMove, { passive: true })
  document.addEventListener('pointerdown', () => { hide(); lastHide = 0 }, true)
  document.addEventListener('pointerleave', () => schedule(null))
  document.addEventListener('focusin', onFocus)
  document.addEventListener('focusout', (e) => { if (e.target === target) hide() })
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && target) hide() }, true)
  document.addEventListener('scroll', () => hide(), true)
  window.addEventListener('blur', () => hide())
  window.addEventListener('resize', () => hide())
  // the host can be removed or re-rendered (a different tip) while shown
  const mo = new MutationObserver((records) => {
    let retarget = false
    for (const r of records) {
      if (r.type !== 'attributes') continue
      const n = r.target as Element
      if (r.attributeName === 'title') {
        if (n.hasAttribute('title')) adopt(n)
        // the framework cleared a title we had adopted (our own adopt() also removes the
        // title, but that record's oldValue is the text we moved)
        else if (adopted.has(n) && r.oldValue !== null && r.oldValue !== n.getAttribute('data-tip')) {
          n.removeAttribute('data-tip')
          adopted.delete(n)
        }
      }
      if (n === target) retarget = true
    }
    if (!target) return
    if (!target.isConnected || !target.getAttribute('data-tip')) return hide()
    if (retarget) { render(target); place(target) }
  })
  mo.observe(document.body, { subtree: true, childList: true, attributes: true, attributeOldValue: true, attributeFilter: ['title', 'data-tip', 'data-tip-key'] })
}
