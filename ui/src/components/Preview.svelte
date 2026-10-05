<script lang="ts">
  import { proxyUrl, url } from '../lib/api'
  import { app } from '../lib/store.svelte'
  import { dialogs } from '../lib/dialogs.svelte'
  import { drawFills, drawRuns, rectContains } from '../lib/render'
  import type { Rect } from '../lib/types'
  import FaceEditor from './FaceEditor.svelte'
  import Icon from './Icon.svelte'
  import { tick, untrack } from 'svelte'
  import { LOUPE_SRC, loupeWindow } from '../lib/loupe'

  let { tool = $bindable('pan') }: { tool?: 'pan' | 'edge' | 'brush-add' | 'brush-remove' } = $props()

  const s = $derived(app.session)
  const lay = $derived(s?.layout)
  const info = $derived(s?.meta?.info)

  let host: HTMLDivElement
  let beforeCv = $state<HTMLCanvasElement>()!
  let afterCv = $state<HTMLCanvasElement>()!
  let paneW = $state(400)
  let paneH = $state(300)
  let stacked = $state(false)
  // pane arrangement: 'auto' picks side by side or stacked from the photo's shape.
  // Before and After are always both visible (spec: "Always side by side"); there is no After-only mode.
  type Arrange = 'auto' | 'side' | 'stacked'
  const ARRANGE_KEY = 'photoband.previewLayout'
  let arrange = $state<Arrange>(loadArrange())
  function loadArrange(): Arrange {
    try {
      const v = localStorage.getItem(ARRANGE_KEY)
      if (v === 'side' || v === 'stacked' || v === 'auto') return v
      // 'after' was an older After-only mode: it falls back to side by side
      if (v === 'after') return 'side'
    } catch {
      /* storage unavailable */
    }
    return 'auto'
  }
  function setArrange(a: Arrange) {
    arrange = a
    try {
      localStorage.setItem(ARRANGE_KEY, a)
    } catch {
      /* storage unavailable */
    }
    measure()
  }
  let zoom = $state(0.1) // CSS px per output px
  let panX = $state(0) // CSS px offset of output (0,0) inside a pane
  let panY = $state(0)
  let fitMode = $state(true)
  let brushSize = $state(24) // CSS px
  let dpr = window.devicePixelRatio || 1

  // images
  const imgs = new Map<string, HTMLImageElement>()
  let imgTick = $state(0)
  // a load that failed is retried (after 1.5 s, then 3 s) and then given up: never cached as a
  // broken image for good, never a spinner that turns forever
  const IMG_TRIES = 3
  const imgFails = new Map<string, { n: number; at: number }>()
  function imgFailed(src: string) {
    return (imgFails.get(src)?.n ?? 0) >= IMG_TRIES
  }
  function img(src: string): HTMLImageElement | null {
    let im = imgs.get(src)
    if (!im) {
      const f = imgFails.get(src)
      if (f && (f.n >= IMG_TRIES || performance.now() - f.at < 1500 * f.n)) return null
      im = new Image()
      im.decoding = 'async'
      im.onload = () => {
        imgFails.delete(src)
        imgTick++
      }
      im.onerror = () => {
        if (imgs.get(src) === im) imgs.delete(src)
        const prev = imgFails.get(src)
        imgFails.set(src, { n: (prev?.n ?? 0) + 1, at: performance.now() })
        imgTick++
      }
      im.src = src
      imgs.set(src, im)
      if (imgs.size > 40) imgs.delete(imgs.keys().next().value!)
    }
    return im.complete && im.naturalWidth ? im : null
  }

  // detail crops when zoomed past the proxy's resolution
  let detail = $state<{ key: string; img: HTMLImageElement; rect: Rect } | null>(null)
  let detailTimer: any
  let detailPending = $state(false)

  // mapping source → output
  function map(): { dx: number; dy: number } {
    if (!lay) return { dx: 0, dy: 0 }
    if (lay.mode === 'erase') return { dx: 0, dy: 0 }
    return { dx: lay.photoRect[0] - lay.sourceRect[0], dy: lay.photoRect[1] - lay.sourceRect[1] }
  }

  // --- sizing & fit -----------------------------------------------------------------
  $effect(() => {
    const ro = new ResizeObserver(() => measure())
    ro.observe(host)
    return () => ro.disconnect()
  })

  const STACK_BIAS = 1.35 // stacked must fit the photo 35% larger than side by side to win
  const HYSTERESIS = 1.1 // and the other arrangement must be 10% better to switch back and forth

  function measure() {
    if (!host) return
    const r = host.getBoundingClientRect()
    const cw = lay?.canvas[0] ?? 3
    const ch = lay?.canvas[1] ?? 2
    const aspect = cw / ch
    if (arrange === 'side' || arrange === 'stacked') {
      stacked = arrange === 'stacked'
    } else {
      // displayed photo height in each arrangement
      const sideScore = Math.max(1, Math.min((r.width / 2 - 56) / aspect, r.height - 56))
      const stackScore = Math.max(1, Math.min((r.width - 56) / aspect, r.height / 2 - 56))
      const ratio = stackScore / (sideScore * STACK_BIAS)
      if (!stacked && ratio > HYSTERESIS) stacked = true
      else if (stacked && ratio < 1 / HYSTERESIS) stacked = false
    }
    paneW = stacked ? r.width : r.width / 2
    paneH = stacked ? r.height / 2 : r.height
    dpr = window.devicePixelRatio || 1
    if (fitMode) fit()
  }

  function fit() {
    if (!lay) return
    const [cw, ch] = lay.canvas
    const m = 28
    // the zoom controls live in the status bar below, so the whole pane is available
    zoom = Math.max(0.001, Math.min((paneW - 2 * m) / cw, (paneH - 2 * m) / ch))
    panX = (paneW - cw * zoom) / 2
    panY = (paneH - ch * zoom) / 2
    fitMode = true
  }

  $effect(() => {
    void [arrange, tool]
    untrack(() => measure())
  })

  function cycleArrange() {
    const order: Arrange[] = ['side', 'stacked']
    const cur: Arrange = stacked ? 'stacked' : 'side'
    setArrange(order[(order.indexOf(cur) + 1) % order.length])
  }

  $effect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'y' && e.key !== 'Y') return
      if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return
      const t = e.target as HTMLElement | null
      if (t && (t.isContentEditable || t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return
      if (dialogs.confirmState || dialogs.browserState || dialogs.settingsOpen || dialogs.helpOpen) return
      e.preventDefault()
      cycleArrange()
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  })

  function setZoom(z: number, cx = paneW / 2, cy = paneH / 2) {
    z = Math.max(0.005, Math.min(8, z))
    const ox = (cx - panX) / zoom
    const oy = (cy - panY) / zoom
    zoom = z
    panX = cx - ox * z
    panY = cy - oy * z
    fitMode = false
  }

  export function zoomFit() {
    fit()
  }
  export function zoom100() {
    setZoom(1 / dpr)
  }
  /** Side by side or stacked (the command palette and the status-bar buttons). */
  export function arrangeTo(a: 'side' | 'stacked') {
    setArrange(a)
  }

  // refit when the photo or canvas size changes
  let lastKey = ''
  let lastPath: string | undefined
  $effect(() => {
    const k = s?.path + '|' + lay?.canvas.join('x') + '|' + lay?.mode
    if (k !== lastKey) {
      lastKey = k
      // another photo always opens fitted; the same photo keeps its zoom unless it was fitted
      if (fitMode || s?.path !== lastPath) fitMode = true
      lastPath = s?.path
      measure()
    }
  })

  // --- drawing -----------------------------------------------------------------------
  let raf = 0
  function schedule() {
    if (!raf) raf = requestAnimationFrame(draw)
  }
  $effect(() => {
    // redraw on any of these
    void [lay, zoom, panX, panY, paneW, paneH, imgTick, app.showFaces, app.showOriginal, detail, s?.existing, s?.draft.photoRect, s?.draft.mode, s?.draft.existingChoice, s?.existingIgnored, edgeDrag, brushMask, app.fontsVersion, stacked, tool, s?.erasePreview, s?.draft.meta, app.selectedFace, app.hoverFace]
    schedule()
  })

  function prepCanvas(cv: HTMLCanvasElement) {
    const w = Math.round(paneW * dpr)
    const h = Math.round(paneH * dpr)
    if (cv.width !== w || cv.height !== h) {
      cv.width = w
      cv.height = h
    }
    const ctx = cv.getContext('2d')!
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, paneW, paneH)
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    return ctx
  }

  // Case B/C before the user has chosen what to do with the caption already in the photo:
  // don't stack a new frame around the old band; show the file as is with a prompt.
  // ("Ignore"/"Dismiss" in the banner sets existingIgnored: then the new band is wanted)
  const undecided = $derived(!!s && (s.existing?.case === 'B' || s.existing?.case === 'C') && s.draft.mode === 'band' && !s.draft.existingChoice && !s.existingIgnored)

  function drawPlaceholder(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
    ctx.save()
    ctx.fillStyle = 'rgba(0,0,0,.14)'
    ctx.fillRect(x, y, w, h)
    const cx = Math.min(Math.max(x + w / 2, 20), paneW - 20)
    const cy = Math.min(Math.max(y + h / 2, 20), paneH - 20)
    const a = (performance.now() / 700) * Math.PI * 2
    ctx.lineWidth = 3
    ctx.lineCap = 'round'
    ctx.strokeStyle = 'rgba(255,255,255,.25)'
    ctx.beginPath()
    ctx.arc(cx, cy, 12, 0, Math.PI * 2)
    ctx.stroke()
    ctx.strokeStyle = 'rgba(255,255,255,.9)'
    ctx.beginPath()
    ctx.arc(cx, cy, 12, a, a + Math.PI * 0.6)
    ctx.stroke()
    ctx.restore()
  }

  function drawChip(ctx: CanvasRenderingContext2D, text: string, short = '') {
    ctx.save()
    ctx.font = '600 13px ' + getComputedStyle(document.body).fontFamily
    // never clipped: a shorter text in a narrow pane
    if (ctx.measureText(text).width + 28 > paneW - 16 && short) text = short
    const tw = Math.min(ctx.measureText(text).width + 28, paneW - 16)
    const x = Math.max(8, (paneW - tw) / 2)
    const y = Math.max(40, Math.min(paneH - 44, panY + ((lay?.canvas[1] ?? 0) * zoom) / 2 - 16))
    ctx.fillStyle = 'rgba(20,20,22,.78)'
    ctx.beginPath()
    if ((ctx as any).roundRect) (ctx as any).roundRect(x, y, tw, 32, 16)
    else ctx.rect(x, y, tw, 32)
    ctx.fill()
    ctx.fillStyle = '#fff'
    ctx.textBaseline = 'middle'
    ctx.fillText(text, x + 14, y + 16)
    ctx.restore()
  }

  let loading = $state(false)

  function draw() {
    raf = 0
    if (!s || !lay || !info || !beforeCv || !afterCv) return
    const proxySrc = proxyUrl(s.path, s.proxyVersion)
    const proxy = img(proxySrc)
    const proxyFailed = !proxy && imgFailed(proxySrc)
    loading = !proxy && !proxyFailed
    const W = info.upright_width
    const H = info.upright_height
    const ps = proxy ? proxy.naturalWidth / W : 1
    const { dx, dy } = map()
    // --- before: the file as it is now
    {
      const ctx = prepCanvas(beforeCv)
      const ox = panX + dx * zoom
      const oy = panY + dy * zoom
      if (proxy) ctx.drawImage(proxy, ox, oy, W * zoom, H * zoom)
      else drawPlaceholder(ctx, ox, oy, W * zoom, H * zoom)
      drawDetail(ctx, ox, oy, [0, 0, W, H])
      drawExistingOverlays(ctx, ox, oy)
      // (faces on Before are drawn by the face editor, over this canvas)
      drawMaskOverlay(ctx, ox, oy)
    }
    // --- after: the output
    {
      const ctx = prepCanvas(afterCv)
      if (app.showOriginal || undecided) {
        const ox = panX + dx * zoom
        const oy = panY + dy * zoom
        if (proxy) ctx.drawImage(proxy, ox, oy, W * zoom, H * zoom)
        else drawPlaceholder(ctx, ox, oy, W * zoom, H * zoom)
        drawDetail(ctx, ox, oy, [0, 0, W, H])
        if (!app.showOriginal) {
          if (app.showFaces) drawFaces(ctx, ox, oy, [0, 0, W, H])
          drawChip(ctx, 'Choose how to handle the existing caption', 'Choose an option above')
        }
      } else {
        ctx.save()
        ctx.beginPath()
        ctx.rect(panX, panY, lay.canvas[0] * zoom, lay.canvas[1] * zoom)
        ctx.clip()
        if (lay.mode === 'erase') {
          const src = s.erasePreview ? img(s.erasePreview) : proxy
          const im = src || proxy
          if (im) ctx.drawImage(im, panX, panY, lay.canvas[0] * zoom, lay.canvas[1] * zoom)
          else drawPlaceholder(ctx, panX, panY, lay.canvas[0] * zoom, lay.canvas[1] * zoom)
        } else {
          drawFills(ctx, lay, zoom, panX, panY)
          const [sx, sy, sw, sh] = lay.sourceRect
          const px = panX + lay.photoRect[0] * zoom
          const py = panY + lay.photoRect[1] * zoom
          if (proxy) ctx.drawImage(proxy, sx * ps, sy * ps, sw * ps, sh * ps, px, py, sw * zoom, sh * zoom)
          else drawPlaceholder(ctx, px, py, sw * zoom, sh * zoom)
          drawDetail(ctx, panX + dx * zoom, panY + dy * zoom, lay.sourceRect)
        }
        // the saved file never has text over the photo (the backend keeps photo pixels):
        // clip the text to the canvas minus the photo rectangle
        {
          const [px, py, pw, ph] = lay.photoRect
          ctx.beginPath()
          ctx.rect(panX, panY, lay.canvas[0] * zoom, lay.canvas[1] * zoom)
          ctx.rect(panX + px * zoom, panY + py * zoom, pw * zoom, ph * zoom)
          ctx.clip('evenodd')
        }
        drawRuns(ctx, lay.runs, zoom, panX, panY)
        ctx.restore()
        if (app.showFaces) drawFaces(ctx, panX + dx * zoom, panY + dy * zoom, lay.sourceRect)
      }
    }
    if (proxyFailed) {
      for (const cv of [beforeCv, afterCv]) drawChip(cv.getContext('2d')!, 'Couldn’t load the preview of this photo. Open another photo and come back to try again.', 'Preview unavailable')
    }
    maybeRequestDetail(ps)
    // keep the spinner turning (and a failed load retrying) until the proxy arrives
    if (loading) schedule()
  }

  function drawDetail(ctx: CanvasRenderingContext2D, ox: number, oy: number, clip: Rect) {
    if (!detail) return
    const [x, y, w, h] = detail.rect
    ctx.save()
    ctx.beginPath()
    ctx.rect(ox + clip[0] * zoom, oy + clip[1] * zoom, clip[2] * zoom, clip[3] * zoom)
    ctx.clip()
    ctx.drawImage(detail.img, ox + x * zoom, oy + y * zoom, w * zoom, h * zoom)
    ctx.restore()
  }

  function maybeRequestDetail(ps: number) {
    if (!s || !info) return
    // screen pixels per source pixel vs proxy pixels per source pixel
    if (zoom * dpr <= ps * 1.25) {
      if (detail) detail = null
      if (detailPending) detailPending = false
      clearTimeout(detailTimer)
      return
    }
    clearTimeout(detailTimer)
    detailTimer = setTimeout(() => {
      const { dx, dy } = map()
      // visible source rect (union of both panes' view, same mapping)
      const x0 = Math.max(0, Math.floor((-panX) / zoom - dx))
      const y0 = Math.max(0, Math.floor((-panY) / zoom - dy))
      const x1 = Math.min(info!.upright_width, Math.ceil((paneW - panX) / zoom - dx))
      const y1 = Math.min(info!.upright_height, Math.ceil((paneH - panY) / zoom - dy))
      if (x1 <= x0 || y1 <= y0) return
      const outW = Math.min(4096, Math.ceil((x1 - x0) * zoom * dpr))
      const key = `${s!.path}|${x0},${y0},${x1},${y1}|${outW}`
      if (detail?.key === key) return
      const im = new Image()
      detailPending = true
      im.onload = () => {
        detail = { key, img: im, rect: [x0, y0, x1 - x0, y1 - y0] }
        detailPending = false
      }
      im.onerror = () => {
        detailPending = false
      }
      im.src = url('/api/photo/crop', { path: s!.path, x: x0, y: y0, w: x1 - x0, h: y1 - y0, out: outW, v: s!.proxyVersion })
    }, 180)
  }

  function drawFaces(ctx: CanvasRenderingContext2D, ox: number, oy: number, clip: Rect) {
    const faces = s?.meta ? app.faces(s) : null
    if (!faces || !info) return
    const W = info.upright_width
    const H = info.upright_height
    const named = faces.named.filter((f) => f.box)
    const order = [...named].sort((a, b) => a.box![0] + a.box![2] / 2 - (b.box![0] + b.box![2] / 2))
    ctx.save()
    ctx.beginPath()
    ctx.rect(ox + clip[0] * zoom, oy + clip[1] * zoom, clip[2] * zoom, clip[3] * zoom)
    ctx.clip()
    ctx.font = '600 12px ' + getComputedStyle(document.body).fontFamily
    const all = [...named.map((f) => ({ f, n: order.indexOf(f) + 1 })), ...faces.unnamed.filter((f) => f.box).map((f) => ({ f, n: 0 }))]
    // far out, names don't fit: show numbers only
    const boxW = all.map(({ f }) => f.box![2] * W * zoom).sort((a, b) => a - b)
    const numbersOnly = boxW.length > 0 && boxW[Math.floor(boxW.length / 2)] < 44
    const cx0 = ox + clip[0] * zoom
    const cx1 = ox + (clip[0] + clip[2]) * zoom
    for (const { f, n } of all) {
      const [bx, by, bw, bh] = f.box!
      const sel = !!f.key && (f.key === app.selectedFace || f.key === app.hoverFace)
      ctx.lineWidth = sel ? 3 : 2
      ctx.strokeStyle = sel ? '#22d3ee' : n ? '#ffd23f' : 'rgba(255,255,255,.7)'
      ctx.setLineDash(n || sel ? [] : [4, 3])
      ctx.strokeRect(ox + bx * W * zoom, oy + by * H * zoom, bw * W * zoom, bh * H * zoom)
    }
    ctx.setLineDash([])
    // labels: under each box, clamped inside the clip, stacked downward when they collide
    const placed: [number, number, number, number][] = []
    const LH = 19
    for (const { f, n } of all) {
      const [bx, by, bw, bh] = f.box!
      const x = ox + bx * W * zoom
      const y = oy + by * H * zoom
      const w = bw * W * zoom
      const h = bh * H * zoom
      const label = numbersOnly ? (n ? String(n) : '') : n ? `${n}  ${f.name}` : 'Unnamed'
      if (!label) continue
      const tw = ctx.measureText(label).width + 10
      let lx = numbersOnly ? x + w / 2 - tw / 2 : x
      lx = Math.max(cx0, Math.min(cx1 - tw, lx))
      let ly = y + h + 3
      for (let guard = 0; guard < 40; guard++) {
        const hit = placed.find((p) => lx < p[0] + p[2] && lx + tw > p[0] && ly < p[1] + p[3] && ly + LH > p[1])
        if (!hit) break
        ly = hit[1] + hit[3] + 2
      }
      placed.push([lx, ly, tw, LH])
      ctx.fillStyle = 'rgba(0,0,0,.72)'
      ctx.fillRect(lx, ly, tw, LH)
      ctx.fillStyle = '#fff'
      ctx.fillText(label, lx + 5, ly + 14)
    }
    ctx.restore()
  }

  function drawExistingOverlays(ctx: CanvasRenderingContext2D, ox: number, oy: number) {
    const ex = s?.existing
    if (!ex || !s) return
    ctx.save()
    if (ex.textOverPhoto?.length) {
      ctx.strokeStyle = '#ff8a00'
      ctx.lineWidth = 2
      for (const [x, y, w, h] of ex.textOverPhoto) ctx.strokeRect(ox + x * zoom - 3, oy + y * zoom - 3, w * zoom + 6, h * zoom + 6)
    }
    const showEdge = (ex.case === 'B' || ex.case === 'C') && s.draft.mode !== 'band'
    if (showEdge && ex.band) {
      const pr = (edgeDrag?.rect ?? s.draft.photoRect ?? ex.band.photo_rect) as Rect
      ctx.strokeStyle = '#22d3ee'
      ctx.lineWidth = 1.5
      ctx.setLineDash([6, 4])
      ctx.strokeRect(ox + pr[0] * zoom, oy + pr[1] * zoom, pr[2] * zoom, pr[3] * zoom)
      ctx.setLineDash([])
      for (const b of ex.blocks || []) for (const l of b.lines) {
        ctx.strokeStyle = 'rgba(236,72,153,.9)'
        ctx.lineWidth = 1.25
        ctx.strokeRect(ox + l.box[0] * zoom - 2, oy + l.box[1] * zoom - 2, l.box[2] * zoom + 4, l.box[3] * zoom + 4)
      }
      if (tool === 'edge') {
        ctx.fillStyle = '#22d3ee'
        for (const hnd of handles(pr)) ctx.fillRect(ox + hnd.x * zoom - 5, oy + hnd.y * zoom - 5, 10, 10)
      }
    }
    ctx.restore()
  }

  // --- existing-text: edge handles, loupe, brush --------------------------------------
  let edgeDrag = $state<{ side: string; rect: Rect; start: Rect } | null>(null)
  let selectedEdge = $state<string>('bottom')
  // the loupe shows LOUPE_SRC source px at 4x. src/sx/sy are those of the image on show: a new crop
  // replaces it only once it has loaded, and a failed one leaves a note instead of a broken image
  let loupe = $state<{ x: number; y: number; src: string; sx: number; sy: number; failed: boolean } | null>(null)
  let loupeTimer: any
  let loupeSeq = 0

  function handles(r: Rect) {
    const [x, y, w, h] = r
    return [
      { side: 'top', x: x + w / 2, y },
      { side: 'bottom', x: x + w / 2, y: y + h },
      { side: 'left', x, y: y + h / 2 },
      { side: 'right', x: x + w, y: y + h / 2 },
    ]
  }

  function toSource(e: PointerEvent | MouseEvent, cv: HTMLCanvasElement) {
    const r = cv.getBoundingClientRect()
    const { dx, dy } = map()
    const x = (e.clientX - r.left - panX) / zoom - dx
    const y = (e.clientY - r.top - panY) / zoom - dy
    return { x, y, cx: e.clientX - r.left, cy: e.clientY - r.top }
  }

  function currentEdgeRect(): Rect | null {
    const ex = s?.existing
    if (!ex?.band) return null
    return (s!.draft.photoRect ?? ex.band.photo_rect) as Rect
  }

  function requestLoupe(sx: number, sy: number, cx: number, cy: number) {
    // the loupe follows the pointer at once; its picture follows when loaded
    if (loupe) loupe = { ...loupe, x: cx, y: cy }
    else loupe = { x: cx, y: cy, src: '', sx: 0, sy: 0, failed: false }
    clearTimeout(loupeTimer)
    loupeTimer = setTimeout(() => {
      if (!s || !info) return
      const r = loupeWindow(sx, sy, info.upright_width, info.upright_height)
      const src = url('/api/photo/crop', { path: s.path, x: r.x, y: r.y, w: r.w, h: r.h, out: 160, v: s.proxyVersion })
      const seq = ++loupeSeq
      const im = new Image()
      im.onload = () => {
        if (seq === loupeSeq && loupe) loupe = { ...loupe, src, sx: r.x, sy: r.y, failed: false }
      }
      im.onerror = () => {
        if (seq === loupeSeq && loupe) loupe = { ...loupe, src: '', failed: true }
      }
      im.src = src
    }, 60)
  }
  // a loupe never outlives the drag that opened it (another tool, another photo)
  $effect(() => {
    void [tool, s?.path]
    untrack(() => {
      if (!edgeDrag && loupe) {
        clearTimeout(loupeTimer)
        loupeSeq++
        loupe = null
      }
    })
  })

  // brush mask at proxy resolution
  let brushMask = $state(0)
  let maskAdd: HTMLCanvasElement | null = null
  let maskRem: HTMLCanvasElement | null = null
  function ensureMasks() {
    if (!s || !info) return
    const proxy = img(proxyUrl(s.path, s.proxyVersion))
    const w = proxy?.naturalWidth || Math.round(info.upright_width / 4)
    const h = proxy?.naturalHeight || Math.round(info.upright_height / 4)
    for (const k of ['add', 'rem'] as const) {
      let c = k === 'add' ? maskAdd : maskRem
      if (!c || c.width !== w || c.height !== h) {
        c = document.createElement('canvas')
        c.width = w
        c.height = h
        const data = k === 'add' ? s.draft.brushAdd : s.draft.brushRemove
        if (data) {
          const im = new Image()
          im.onload = () => {
            c!.getContext('2d')!.drawImage(im, 0, 0, w, h)
            brushMask++
          }
          im.src = data
        }
        if (k === 'add') maskAdd = c
        else maskRem = c
      }
    }
  }
  let lastMaskPath = ''
  $effect(() => {
    if (s?.path !== lastMaskPath) {
      lastMaskPath = s?.path ?? ''
      maskAdd = maskRem = null
      imgFails.clear()   // coming back to a photo tries its failed images again
    }
  })

  function paint(sx: number, sy: number, add: boolean) {
    ensureMasks()
    if (!info) return
    const c = add ? maskAdd! : maskRem!
    const other = add ? maskRem! : maskAdd!
    const k = c.width / info.upright_width
    const r = (brushSize / zoom) * k / 2
    for (const [cv, mode] of [[c, 'source-over'], [other, 'destination-out']] as const) {
      const ctx = cv.getContext('2d')!
      ctx.globalCompositeOperation = mode
      ctx.fillStyle = '#fff'
      ctx.beginPath()
      ctx.arc(sx * k, sy * k, Math.max(0.5, r), 0, Math.PI * 2)
      ctx.fill()
    }
    brushMask++
  }

  function drawMaskOverlay(ctx: CanvasRenderingContext2D, ox: number, oy: number) {
    if (!s || s.draft.mode !== 'erase' || !info) return
    ensureMasks()
    ctx.save()
    ctx.globalAlpha = 0.45
    for (const [c, color] of [[maskAdd, '#ec4899'], [maskRem, '#22c55e']] as const) {
      if (!c) continue
      const tint = document.createElement('canvas')
      tint.width = c.width
      tint.height = c.height
      const t = tint.getContext('2d')!
      t.drawImage(c, 0, 0)
      t.globalCompositeOperation = 'source-in'
      t.fillStyle = color
      t.fillRect(0, 0, c.width, c.height)
      ctx.drawImage(tint, ox, oy, info.upright_width * zoom, info.upright_height * zoom)
    }
    ctx.restore()
  }

  function commitMasks() {
    if (!s) return
    const toData = (c: HTMLCanvasElement | null) => {
      if (!c) return null
      const d = c.getContext('2d')!.getImageData(0, 0, c.width, c.height).data
      for (let i = 3; i < d.length; i += 4) if (d[i]) return c.toDataURL('image/png')
      return null
    }
    s.draft.brushAdd = toData(maskAdd)
    s.draft.brushRemove = toData(maskRem)
    app.commit(s)
  }

  // --- pointer ---------------------------------------------------------------------
  let dragging: { x: number; y: number; px: number; py: number } | null = null
  let painting: boolean | null = null

  /** The face under a point (source pixels), with its left-to-right number (0 = unnamed). */
  function faceAt(x: number, y: number): { name: string; n: number; total: number; key?: string } | null {
    const faces = s?.meta ? app.faces(s) : null
    if (!faces || !info) return null
    const W = info.upright_width
    const H = info.upright_height
    const named = faces.named.filter((f) => f.box)
    const order = [...named].sort((a, b) => a.box![0] + a.box![2] / 2 - (b.box![0] + b.box![2] / 2))
    const inside = (b: number[]) => x >= b[0] * W && x <= (b[0] + b[2]) * W && y >= b[1] * H && y <= (b[1] + b[3]) * H
    for (const f of named) if (inside(f.box!)) return { name: f.name, n: order.indexOf(f) + 1, total: order.length, key: f.key }
    for (const f of faces.unnamed) if (f.box && inside(f.box)) return { name: '', n: 0, total: order.length, key: f.key }
    return null
  }

  function explainFace(f: { name: string; n: number; total: number }) {
    if (!f.n) app.toast('info', 'This face has no name, so it is not in the caption. Click its label on Before to name it.', undefined, 6000)
    else app.toast('info', `${f.name}: person ${f.n} of ${f.total}, counting from the left. Names in the caption follow this order.`, undefined, 6000)
  }

  let downAt: { x: number; y: number } | null = null
  function down(e: PointerEvent, pane: 'before' | 'after') {
    const cv = pane === 'before' ? beforeCv : afterCv
    cv.setPointerCapture(e.pointerId)
    const p = toSource(e, cv)
    downAt = { x: e.clientX, y: e.clientY }
    bandHover = null
    if (pane === 'before' && (tool === 'brush-add' || tool === 'brush-remove') && s?.draft.mode === 'erase') {
      painting = tool === 'brush-add'
      paint(p.x, p.y, painting)
      return
    }
    if (pane === 'before' && tool === 'edge') {
      const r = currentEdgeRect()
      if (r) {
        const tol = 10 / zoom
        const [x, y, w, h] = r
        let side = ''
        if (Math.abs(p.y - y) < tol && p.x > x - tol && p.x < x + w + tol) side = 'top'
        else if (Math.abs(p.y - (y + h)) < tol && p.x > x - tol && p.x < x + w + tol) side = 'bottom'
        else if (Math.abs(p.x - x) < tol && p.y > y - tol && p.y < y + h + tol) side = 'left'
        else if (Math.abs(p.x - (x + w)) < tol && p.y > y - tol && p.y < y + h + tol) side = 'right'
        if (side) {
          selectedEdge = side
          edgeDrag = { side, rect: [...r] as Rect, start: [...r] as Rect }
          requestLoupe(p.x, p.y, p.cx, p.cy)
          return
        }
      }
    }
    if (pane === 'after' && lay && tool === 'pan') {
      // click on text focuses its block editor
      const ox = p.x + map().dx
      const oy = p.y + map().dy
      for (const [id, box] of Object.entries(lay.blockBoxes)) {
        if (rectContains(box, ox, oy)) {
          app.focusBlock = id
          app.inspectorTab = 'text'
          break
        }
      }
    }
    dragging = { x: e.clientX, y: e.clientY, px: panX, py: panY }
  }

  function move(e: PointerEvent, pane: 'before' | 'after') {
    const cv = pane === 'before' ? beforeCv : afterCv
    if (painting !== null) {
      const p = toSource(e, cv)
      paint(p.x, p.y, painting)
      return
    }
    if (edgeDrag && info) {
      const p = toSource(e, cv)
      const [x, y, w, h] = edgeDrag.start
      let r: Rect = [x, y, w, h]
      const X = Math.round(p.x)
      const Y = Math.round(p.y)
      if (edgeDrag.side === 'top') r = [x, Math.min(Y, y + h - 8), w, h - (Math.min(Y, y + h - 8) - y)]
      if (edgeDrag.side === 'bottom') r = [x, y, w, Math.max(8, Y - y)]
      if (edgeDrag.side === 'left') r = [Math.min(X, x + w - 8), y, w - (Math.min(X, x + w - 8) - x), h]
      if (edgeDrag.side === 'right') r = [x, y, Math.max(8, X - x), h]
      r = clampRect(r, info.upright_width, info.upright_height)
      edgeDrag = { ...edgeDrag, rect: r }
      requestLoupe(p.x, p.y, p.cx, p.cy)
      return
    }
    if (dragging) {
      panX = dragging.px + (e.clientX - dragging.x)
      panY = dragging.py + (e.clientY - dragging.y)
      fitMode = false
    }
  }

  function up(e?: PointerEvent, pane?: 'before' | 'after') {
    // a click (not a drag) on a face in faces mode says whose face it is
    if (e && pane && downAt && app.showFaces && tool === 'pan' && Math.hypot(e.clientX - downAt.x, e.clientY - downAt.y) < 4) {
      if (pane === 'before') app.selectedFace = null   // a click beside the faces
      else {
        const p = toSource(e, afterCv)
        const f = faceAt(p.x, p.y)
        if (f) {
          app.selectedFace = f.key ?? null
          explainFace(f)
        }
      }
    }
    downAt = null
    if (painting !== null) {
      painting = null
      commitMasks()
    }
    if (edgeDrag && s) {
      app.setPhotoEdge(s, edgeDrag.rect)
      edgeDrag = null
    }
    if (loupe) {
      clearTimeout(loupeTimer)
      loupeSeq++
      loupe = null
    }
    dragging = null
  }

  // the caption area under the pointer in After (screen px inside the pane), for the hover outline
  let bandHover = $state<[number, number, number, number] | null>(null)
  function captionRect(): Rect | null {
    if (!lay) return null
    const boxes = Object.values(lay.blockBoxes)
    if (!boxes.length) return null
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
    for (const [x, y, w, h] of boxes) {
      x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + w); y1 = Math.max(y1, y + h)
    }
    return [x0, y0, x1 - x0, y1 - y0]
  }
  function hoverBand(e: PointerEvent) {
    if (tool !== 'pan' || dragging || app.showOriginal || undecided || !lay) {
      if (bandHover) bandHover = null
      return
    }
    const p = toSource(e, afterCv)
    const ox = p.x + map().dx
    const oy = p.y + map().dy
    const over = Object.values(lay.blockBoxes).some((b) => rectContains(b, ox, oy))
    const r = over ? captionRect() : null
    if (!r) {
      if (bandHover) bandHover = null
      return
    }
    const pad = 6
    bandHover = [panX + r[0] * zoom - pad, panY + r[1] * zoom - pad, r[2] * zoom + 2 * pad, r[3] * zoom + 2 * pad]
  }
  $effect(() => {
    // zoom, pan and layout changes move the caption: drop the outline until the pointer moves again
    void [zoom, panX, panY, lay]
    untrack(() => { if (bandHover) bandHover = null })
  })

  function wheel(e: WheelEvent, pane: 'before' | 'after') {
    e.preventDefault()
    const cv = pane === 'before' ? beforeCv : afterCv
    const r = cv.getBoundingClientRect()
    const f = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0022))
    setZoom(zoom * f, e.clientX - r.left, e.clientY - r.top)
  }

  export function nudgeEdge(dx: number, dy: number) {
    const r = currentEdgeRect()
    if (!r || !s || !info) return false
    let [x, y, w, h] = r
    if (selectedEdge === 'top') { y += dy; h -= dy }
    if (selectedEdge === 'bottom') h += dy
    if (selectedEdge === 'left') { x += dx; w -= dx }
    if (selectedEdge === 'right') w += dx
    app.setPhotoEdge(s, clampRect([x, y, w, h], info.upright_width, info.upright_height))
    return true
  }

  function clampRect(r: Rect, W: number, H: number): Rect {
    let [x, y, w, h] = r.map(Math.round) as Rect
    x = Math.max(0, Math.min(W - 8, x))
    y = Math.max(0, Math.min(H - 8, y))
    w = Math.max(8, Math.min(W - x, w))
    h = Math.max(8, Math.min(H - y, h))
    return [x, y, w, h]
  }

  // erase preview from the backend, refreshed when the mask or edge changes
  let erTimer: any
  let erFailNote = ''
  function erasePreviewFailed(detail: string) {
    // After keeps the last preview that worked: say that it is out of date (once per message)
    const msg = `Couldn’t update the erase preview${detail ? `: ${detail}` : ''}. After may not show your latest changes.`
    if (msg !== erFailNote) app.toast('warn', msg)
    erFailNote = msg
  }
  $effect(() => {
    const cur = s
    if (!cur || cur.draft.mode !== 'erase' || !cur.existing?.band) return
    void [cur.draft.photoRect, cur.draft.brushAdd, cur.draft.brushRemove]
    clearTimeout(erTimer)
    erTimer = setTimeout(async () => {
      try {
        const r = await fetch('/api/photo/erase-preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Photoband-Token': (await import('../lib/api')).TOKEN },
          body: JSON.stringify({ path: cur.path, erase: { band: cur.existing!.band, photoRect: cur.draft.photoRect ?? cur.existing!.band!.photo_rect, blocks: cur.existing!.blocks || [], brushAdd: cur.draft.brushAdd, brushRemove: cur.draft.brushRemove, grow: 2 } }),
        })
        if (r.ok) {
          const old = cur.erasePreview
          cur.erasePreview = URL.createObjectURL(await r.blob())
          if (old) setTimeout(() => URL.revokeObjectURL(old), 2000)
          erFailNote = ''
        } else {
          const detail = await r.json().then((j) => (typeof j?.detail === 'string' ? j.detail : '')).catch(() => '')
          erasePreviewFailed(detail)
        }
      } catch {
        erasePreviewFailed('')
      }
    }, 250)
  })

  // output pixel size, and the print size at the file's DPI (two lines in a narrow window)
  const sizeParts = $derived.by(() => {
    if (!lay || !info) return null
    const [w, h] = lay.canvas
    const dpi = info.dpi?.[0]
    const px = `${w.toLocaleString()} × ${h.toLocaleString()} px`
    const print = dpi && dpi > 72.5 ? `${(w / dpi).toFixed(2)} × ${(h / dpi).toFixed(2)} in at ${Math.round(dpi)} dpi` : ''
    return { px, print }
  })
  const zoomText = $derived(`${Math.round(zoom * dpr * 100)}%`)

  // the edge being dragged, in loupe pixels (the loupe shows 40 source px at 4x)
  const loupeLine = $derived.by(() => {
    if (!loupe || !edgeDrag) return null
    const [x, y, w, h] = edgeDrag.rect
    const k = 160 / LOUPE_SRC
    if (edgeDrag.side === 'top') return { horiz: true, at: (y - loupe.sy) * k }
    if (edgeDrag.side === 'bottom') return { horiz: true, at: (y + h - loupe.sy) * k }
    if (edgeDrag.side === 'left') return { horiz: false, at: (x - loupe.sx) * k }
    return { horiz: false, at: (x + w - loupe.sx) * k }
  })
  const effArrange = $derived(stacked ? 'stacked' : 'side')

  // --- face editor ------------------------------------------------------------------------
  let faceEd = $state<FaceEditor | undefined>()
  // where the source image sits on Before (the same mapping as map(), kept reactive)
  const beforeShift = $derived.by(() => {
    if (!lay || lay.mode === 'erase') return { dx: 0, dy: 0 }
    return { dx: lay.photoRect[0] - lay.sourceRect[0], dy: lay.photoRect[1] - lay.sourceRect[1] }
  })
  function toggleAddFace() {
    app.faceTool = app.faceTool === 'add' ? 'select' : 'add'
    if (app.faceTool === 'select') app.faceToolTarget = null
  }
  /** N: start drawing a face (with the mouse), or, from the keyboard, put one in the middle. */
  export async function addFace(fromKeyboard: boolean) {
    if (tool !== 'pan' || !s?.meta) return   // the edge and brush tools own the photo
    if (!app.showFaces) {
      app.showFaces = true
      await tick()   // the face layer mounts
    }
    if (fromKeyboard && faceEd) faceEd.addAtCenter()
    else app.faceTool = 'add'
  }
  // never left drawing on another photo or with another tool
  $effect(() => {
    void s?.path
    void tool
    untrack(() => {
      if (app.faceTool === 'add') {
        app.faceTool = 'select'
        app.faceToolTarget = null
      }
      app.selectedFace = null
    })
  })
</script>

<div class="preview-wrap">
  <div class="preview" class:stacked bind:this={host}>
    {#each ['before', 'after'] as pane (pane)}
      <div class="pane" style="width:{paneW}px;height:{paneH}px" onwheel={(e) => wheel(e, pane as 'before' | 'after')}>
        <div class="tag">{pane === 'before' ? 'Before' : app.showOriginal ? 'After — showing original' : 'After'}</div>
        {#if pane === 'before'}
          <canvas bind:this={beforeCv} tabindex="0" style="width:{paneW}px;height:{paneH}px" class:edge={tool === 'edge'} class:brush={tool.startsWith('brush')}
            onpointerdown={(e) => down(e, 'before')} onpointermove={(e) => move(e, 'before')} onpointerup={(e) => up(e, 'before')} onpointercancel={() => up()}
            aria-label="Before: the file as it is now"></canvas>
          {#if app.showFaces && s && info && lay}
            <FaceEditor bind:this={faceEd} {s} ox={panX + beforeShift.dx * zoom} oy={panY + beforeShift.dy * zoom} {zoom} W={info.upright_width} H={info.upright_height} inert={tool !== 'pan'} />
          {/if}
        {:else}
          <canvas bind:this={afterCv} style="width:{paneW}px;height:{paneH}px" class:over-band={!!bandHover} data-tip={bandHover ? 'Click to edit the caption' : undefined}
            onpointerdown={(e) => down(e, 'after')} onpointermove={(e) => { move(e, 'after'); hoverBand(e) }} onpointerleave={() => (bandHover = null)} onpointerup={(e) => up(e, 'after')} onpointercancel={() => up()}
            aria-label="After: the output as it will be saved"></canvas>
        {/if}
        {#if pane === 'after' && bandHover}
          <div class="band-hover" aria-hidden="true" style="left:{bandHover[0]}px;top:{bandHover[1]}px;width:{bandHover[2]}px;height:{bandHover[3]}px"></div>
        {/if}
      </div>
    {/each}
    {#if loupe}
      <div class="loupe" style="left:{Math.min(loupe.x + 24, paneW - 180)}px;top:{Math.max(8, loupe.y - 180)}px">
        {#if loupe.src}
          <img src={loupe.src} alt="Edge at 400%" />
        {:else}
          <div class="loupe-note">{loupe.failed ? 'Can’t show this spot' : 'Loading…'}</div>
        {/if}
        {#if loupeLine && loupe.src}
          <div class="edge-line" class:horiz={loupeLine.horiz} style={loupeLine.horiz ? `top:${loupeLine.at}px` : `left:${loupeLine.at}px`}></div>
        {/if}
        <span>400%</span>
      </div>
    {/if}
  </div>
  <div class="status row" role="toolbar" aria-label="Preview">
    <button class="btn sm ghost" aria-label="Zoom to fit" data-tip="Fit the whole photo in view" data-tip-key="Mod+0" data-tip-side="top" onclick={zoomFit}>Fit</button>
    <button class="btn sm ghost" aria-label="Zoom 1:1" data-tip="Actual pixels: one pixel of the saved file per screen pixel" data-tip-key="Mod+1" data-tip-side="top" onclick={zoom100}>1:1</button>
    <button class="btn sm ghost icon" aria-label="Zoom out" data-tip="Zoom out. Or scroll over the photo." data-tip-side="top" onclick={() => setZoom(zoom / 1.25)}><Icon name="zoomout" size={14} /></button>
    <span class="zoom" aria-live="polite" data-tip="Current zoom. 100% shows the saved file pixel for pixel." data-tip-side="top"><span class="zl">{'Zoom '}</span>{zoomText}</span>
    <button class="btn sm ghost icon" aria-label="Zoom in" data-tip="Zoom in. Or scroll over the photo." data-tip-side="top" onclick={() => setZoom(zoom * 1.25)}><Icon name="zoomin" size={14} /></button>
    <span class="sep"></span>
    <div class="seg" role="group" aria-label="Pane layout">
      <button class="btn sm ghost" class:on={effArrange === 'side'} aria-pressed={effArrange === 'side'} aria-label="Side by side" data-tip="Before and after side by side" data-tip-key="Y" data-tip-side="top" onclick={() => setArrange('side')}><Icon name="split" size={13} /><span class="lbl">Side by side</span></button>
      <button class="btn sm ghost" class:on={effArrange === 'stacked'} aria-pressed={effArrange === 'stacked'} aria-label="Stacked" data-tip="Before above after" data-tip-key="Y" data-tip-side="top" onclick={() => setArrange('stacked')}><Icon name="stack" size={13} /><span class="lbl">Stacked</span></button>
    </div>
    <span class="sep"></span>
    <button class="btn sm ghost faces" class:on={app.showFaces} aria-pressed={app.showFaces} aria-label={app.showFaces ? 'Hide faces' : 'Show faces'} data-tip={app.showFaces ? 'Hide the face tags' : 'Show the face tags: click one to rename, move or remove it'} data-tip-key="F" data-tip-side="top" onclick={() => (app.showFaces = !app.showFaces)}><Icon name="faces" size={13} /><span class="lbl">Faces</span></button>
    {#if app.showFaces}
      <button class="btn sm ghost addface" class:on={app.faceTool === 'add'} aria-pressed={app.faceTool === 'add'} aria-label="Add face" data-tip="Draw a box around a face to tag it" data-tip-key="N" data-tip-side="top" disabled={tool !== 'pan' || !s?.meta} onclick={toggleAddFace}><Icon name="plus" size={13} /><span class="lbl">Add face</span></button>
    {/if}
    <button class="btn sm ghost panels" class:on={app.panelsHidden} aria-pressed={app.panelsHidden} aria-label={app.panelsHidden ? 'Show side panels' : 'Hide side panels'} data-tip="{app.panelsHidden ? 'Show' : 'Hide'} the photo list and the caption panel" data-tip-key="Mod+\" data-tip-side="top" onclick={() => (app.panelsHidden = !app.panelsHidden)}><Icon name="columns" size={13} /><span class="lbl">{app.panelsHidden ? 'Show panels' : 'Hide panels'}</span></button>
    {#if tool.startsWith('brush')}
      <span class="sep"></span>
      <label class="row" data-tip="Brush size" data-tip-side="top">Brush <input type="range" min="4" max="120" bind:value={brushSize} /></label>
    {/if}
    <span class="grow"></span>
    <!-- the size readout has priority; the loading note is short and shrinks instead of pushing it off -->
    {#if sizeParts}<span class="size" data-tip={sizeParts.print ? "Size of the saved file, and its print size at the file's DPI" : 'Size of the saved file. It has no print size (DPI) set.'} data-tip-side="top"><span>{sizeParts.px}</span>{#if sizeParts.print}<span class="dotsep">{' · '}</span><span>{sizeParts.print}</span>{/if}</span>{/if}
    {#if loading}<span class="muted loadnote" data-tip="Loading the preview…" data-tip-side="top"><span class="spinner" aria-hidden="true"></span><span class="lbl">Loading…</span></span>{:else if detailPending}<span class="muted loadnote" data-tip="Loading full-resolution detail for this zoom…" data-tip-side="top"><span class="spinner" aria-hidden="true"></span><span class="lbl">Detail…</span></span>{/if}
  </div>
</div>

<style>
  .preview-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; }
  .preview { position: relative; flex: 1; min-height: 0; display: flex; background: var(--preview-bg); overflow: hidden; }
  .preview.stacked { flex-direction: column; }
  .pane { position: relative; overflow: hidden; }
  .pane + .pane { border-left: 1px solid rgba(0, 0, 0, 0.25); }
  .stacked .pane + .pane { border-left: 0; border-top: 1px solid rgba(0, 0, 0, 0.25); }
  canvas { display: block; touch-action: none; cursor: grab; }
  canvas:active { cursor: grabbing; }
  canvas.edge { cursor: crosshair; }
  canvas.brush { cursor: cell; }
  canvas.over-band { cursor: text; }
  /* direct editing is discoverable: a soft outline around the caption on hover */
  .band-hover { position: absolute; pointer-events: none; border-radius: 6px; box-shadow: 0 0 0 1.5px color-mix(in srgb, var(--accent-link) 85%, white), 0 0 0 5px color-mix(in srgb, var(--accent-link) 22%, transparent); animation: bh var(--t-fast) ease-out; z-index: 1; }
  @keyframes bh { from { opacity: 0; } }
  .tag { position: absolute; top: 8px; left: 10px; font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: rgba(255, 255, 255, 0.9); background: rgba(0, 0, 0, 0.35); padding: 2px 7px; border-radius: 4px; pointer-events: none; z-index: 2; }
  .status { flex: none; height: 36px; padding: 0 8px; gap: 2px; font-size: 12px; background: var(--bg-3); border-top: 1px solid var(--line); overflow: hidden; }
  .status .sep { margin: 7px 4px; }
  .status .grow { flex: 1; }
  .status > :global(*) { flex-shrink: 0; }
  .status > .grow { flex-shrink: 1; min-width: 0; }
  .status .muted { color: var(--fg-2); white-space: nowrap; }
  .status > .loadnote { display: inline-flex; align-items: center; gap: 5px; margin-left: 10px; }
  .spinner { width: 10px; height: 10px; border: 2px solid var(--line-2); border-top-color: var(--accent-link); border-radius: 50%; animation: sp 0.8s linear infinite; flex: none; }
  @keyframes sp { to { transform: rotate(360deg) } }
  @media (max-width: 1279px) {
    .status .btn .lbl, .status .loadnote .lbl { display: none; }
    .status .seg .btn, .status .panels, .status .faces, .status .addface { width: 24px; padding: 0; }
    .zoom { min-width: 40px; }
    .zl { display: none; }
    .size { display: flex; flex-direction: column; align-items: flex-end; font-size: 11px; line-height: 1.2; }
    .size .dotsep { display: none; }
  }
  .zoom { min-width: 76px; text-align: center; font-variant-numeric: tabular-nums; color: var(--fg-2); }
  .seg { display: flex; gap: 2px; }
  .seg .btn.on, .status > .btn.on { background: var(--bg-active); color: var(--fg); }
  .size { color: var(--fg-2); font-variant-numeric: tabular-nums; white-space: nowrap; flex-shrink: 0; }
  .loupe .edge-line { position: absolute; top: 0; bottom: 0; width: 1px; background: #22d3ee; box-shadow: 0 0 0 0.5px rgba(0, 0, 0, 0.6); }
  .loupe .edge-line.horiz { top: auto; bottom: auto; left: 0; right: 0; width: auto; height: 1px; }
  .loupe { position: absolute; width: 164px; height: 164px; border: 2px solid #22d3ee; border-radius: 6px; overflow: hidden; background: #000; box-shadow: var(--shadow); pointer-events: none; z-index: 4; }
  .loupe-note { width: 160px; height: 160px; display: grid; place-items: center; font-size: 11px; color: rgba(255, 255, 255, 0.75); }
  .loupe img { width: 160px; height: 160px; image-rendering: pixelated; display: block; }
  .loupe span { position: absolute; right: 4px; bottom: 3px; font-size: 10px; color: #fff; background: rgba(0,0,0,.5); padding: 0 4px; border-radius: 3px; }
</style>
