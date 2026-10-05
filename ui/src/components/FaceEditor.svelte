<script lang="ts">
  // Face tags on the Before pane, editable like a photo organiser: click a face to select it, drag
  // it to move, drag a corner or side to resize, click its name to rename, Delete to remove it.
  // "Add face" (or N) draws a new box. Every change is one undo step and is saved with the photo.
  import { tick } from 'svelte'
  import { app, type PhotoSession } from '../lib/store.svelte'
  import { leftToRight, recentNames, suggestNames, type Box } from '../lib/metaedits'
  import type { Face } from '../lib/types'

  let { s, ox, oy, zoom, W, H, inert = false }: { s: PhotoSession; ox: number; oy: number; zoom: number; W: number; H: number; inert?: boolean } = $props()

  const faces = $derived(app.faces(s))
  const order = $derived(leftToRight(faces.named))
  const all = $derived([...faces.named, ...faces.unnamed].filter((f) => f.box && f.key))
  const MIN_PX = 8 // smallest face box, in image pixels

  // a box being moved or drawn (normalized), shown instead of the saved one until the drag ends
  let live = $state<{ key: string; box: Box } | null>(null)
  let drag: { key: string; kind: 'move' | 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw' | 'new'; start: Box; px: number; py: number; moved: boolean } | null = null
  let host = $state<HTMLDivElement>()!
  // the layer's width, for keeping labels inside it
  let hostW = $state(0)

  const sx = (x: number) => ox + x * W * zoom
  const sy = (y: number) => oy + y * H * zoom
  function boxOf(f: Face): Box {
    return live && live.key === f.key ? live.box : (f.box as Box)
  }
  function toNorm(e: PointerEvent): [number, number] {
    const r = host.getBoundingClientRect()
    return [(e.clientX - r.left - ox) / (W * zoom), (e.clientY - r.top - oy) / (H * zoom)]
  }
  function clamp(b: Box): Box {
    const mw = MIN_PX / W
    const mh = MIN_PX / H
    let [x, y, w, h] = b
    w = Math.max(mw, Math.min(1, w))
    h = Math.max(mh, Math.min(1, h))
    x = Math.max(0, Math.min(1 - w, x))
    y = Math.max(0, Math.min(1 - h, y))
    return [x, y, w, h]
  }

  // ---------------------------------------------------------------- pointer
  function startBox(e: PointerEvent, f: Face, kind: NonNullable<typeof drag>['kind']) {
    if (inert || e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    commitName()
    app.selectedFace = f.key!
    ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
    const [px, py] = toNorm(e)
    drag = { key: f.key!, kind, start: [...(f.box as Box)] as Box, px, py, moved: false }
    focusBox(f.key!)
  }
  function startNew(e: PointerEvent) {
    if (inert || app.faceTool !== 'add' || e.button !== 0) return
    e.stopPropagation()
    e.preventDefault()
    commitName()
    host.setPointerCapture(e.pointerId)
    const [rx, ry] = toNorm(e)
    const px = Math.min(1, Math.max(0, rx))
    const py = Math.min(1, Math.max(0, ry))
    drag = { key: '__new', kind: 'new', start: [px, py, 0, 0], px, py, moved: false }
    live = { key: '__new', box: [px, py, 0, 0] }
  }
  function move(e: PointerEvent) {
    if (!drag) return
    const [x, y] = toNorm(e)
    const dx = x - drag.px
    const dy = y - drag.py
    if (Math.abs(dx) * W * zoom + Math.abs(dy) * H * zoom > 3) drag.moved = true
    const [bx, by, bw, bh] = drag.start
    let b: Box
    if (drag.kind === 'new') {
      // both corners on the photo: a drag that leaves it ends the box at the edge it crossed
      const ex = Math.min(1, Math.max(0, x))
      const ey = Math.min(1, Math.max(0, y))
      live = { key: '__new', box: [Math.min(drag.px, ex), Math.min(drag.py, ey), Math.abs(ex - drag.px), Math.abs(ey - drag.py)] }
      return
    }
    if (drag.kind === 'move') b = [bx + dx, by + dy, bw, bh]
    else {
      let [x0, y0, x1, y1] = [bx, by, bx + bw, by + bh]
      if (drag.kind.includes('w')) x0 = Math.max(0, Math.min(x1 - MIN_PX / W, x0 + dx))
      if (drag.kind.includes('e')) x1 = Math.min(1, Math.max(x0 + MIN_PX / W, x1 + dx))
      if (drag.kind.includes('n')) y0 = Math.max(0, Math.min(y1 - MIN_PX / H, y0 + dy))
      if (drag.kind.includes('s')) y1 = Math.min(1, Math.max(y0 + MIN_PX / H, y1 + dy))
      b = [x0, y0, x1 - x0, y1 - y0]
    }
    live = { key: drag.key, box: clamp(b) }
  }
  async function up() {
    if (!drag) return
    const d = drag
    drag = null
    const b = live?.box
    live = null
    if (d.kind === 'new') {
      if (!b || b[2] * W < MIN_PX || b[3] * H < MIN_PX) return // a click, not a box
      await place(clamp(b))
      return
    }
    if (d.moved && b) app.updateFace(s, d.key, { box: b })
  }

  /** A new box (drawn, or added from the keyboard): for the name being marked, or a new face. */
  async function place(b: Box) {
    const target = app.faceToolTarget
    app.faceTool = 'select'
    app.faceToolTarget = null
    if (target) {
      app.updateFace(s, target, { box: b })
      app.selectedFace = target
      return
    }
    const key = app.addFace(s, b)
    app.selectedFace = key
    await tick()
    openName(key)
  }

  /** Keyboard "Add face": a box in the middle of what is on screen, to move with the arrow keys. */
  export async function addAtCenter() {
    const r = host.getBoundingClientRect()
    const cx = Math.min(1, Math.max(0, (r.width / 2 - ox) / (W * zoom)))
    const cy = Math.min(1, Math.max(0, (r.height / 2 - oy) / (H * zoom)))
    const side = Math.max(MIN_PX, Math.min(W, H) * 0.08)
    await place(clamp([cx - side / W / 2, cy - side / H / 2, side / W, side / H]))
  }

  // ---------------------------------------------------------------- keyboard
  function focusBox(key: string) {
    tick().then(() => host?.querySelector<HTMLElement>(`[data-face="${CSS.escape(key)}"]`)?.focus({ preventScroll: true }))
  }
  function keyBox(e: KeyboardEvent, f: Face) {
    if (inert) return
    const key = f.key!
    const arrows: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }
    if (e.key in arrows) {
      e.preventDefault()
      e.stopPropagation()
      const step = e.shiftKey ? 10 : 1
      const [ax, ay] = arrows[e.key]
      const [x, y, w, h] = f.box as Box
      const b: Box = e.altKey ? [x, y, w + (ax * step) / W, h + (ay * step) / H] : [x + (ax * step) / W, y + (ay * step) / H, w, h]
      app.updateFace(s, key, { box: clamp(b) }, false)
    } else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault()
      e.stopPropagation()
      app.deleteFace(s, key)
    } else if (e.key === 'Enter' || e.key === 'F2') {
      e.preventDefault()
      e.stopPropagation()
      openName(key)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      e.stopPropagation()
      app.selectedFace = null
      ;(e.currentTarget as HTMLElement).blur()
    }
  }

  // ---------------------------------------------------------------- names
  let naming = $state<{ key: string; text: string } | null>(null)
  let pick = $state(0)
  const nameOf = (key: string) => [...faces.named, ...faces.unnamed].find((f) => f.key === key)?.name ?? ''
  const pools = $derived.by(() => {
    const here = new Set(faces.named.map((f) => f.name.toLowerCase()))
    const open = [...app.sessions.values()].flatMap((x) => (x.meta ? app.faces(x).named.map((f) => f.name) : []))
    const kws = app.keywords(s).filter((k) => /^\p{Lu}/u.test(k) && k.includes(' '))
    return [recentNames(), open, kws].map((p) => p.filter((n) => !here.has(n.toLowerCase())))
  })
  const suggestions = $derived(naming ? suggestNames(naming.text, pools, 6) : [])

  async function openName(key: string) {
    if (naming && naming.key !== key) commitName()
    app.selectedFace = key
    naming = { key, text: nameOf(key) }
    pick = -1
    await tick()
    const el = host?.querySelector<HTMLInputElement>('input.namein')
    el?.focus()
    el?.select()
  }
  function commitName(next = false, session: PhotoSession = s) {
    const n = naming
    if (!n) return
    naming = null
    // (the name is for the photo it was typed on: after a photo change `s` is already the next one)
    const cur = [...app.faces(session).named, ...app.faces(session).unnamed].find((f) => f.key === n.key)
    if (cur && n.text.trim() !== cur.name) app.updateFace(session, n.key, { name: n.text })
    if (next) {
      // on to the next face without a name, if any
      const k = faces.unnamed.find((f) => f.key !== n.key && f.box)?.key
      if (k) openName(k)
      else focusBox(n.key)
    }
  }
  function keyName(e: KeyboardEvent) {
    if (e.isComposing) return   // an input method is still composing the name
    if (e.key === 'ArrowDown' && suggestions.length) {
      e.preventDefault()
      pick = (pick + 1) % suggestions.length
    } else if (e.key === 'ArrowUp' && suggestions.length) {
      e.preventDefault()
      pick = pick <= 0 ? suggestions.length - 1 : pick - 1
    } else if (e.key === 'Enter') {
      e.preventDefault()
      if (naming && pick >= 0 && suggestions[pick]) naming.text = suggestions[pick]
      commitName(true)
    } else if (e.key === 'Escape') {
      e.preventDefault()
      const k = naming?.key
      naming = null
      if (k) focusBox(k)
    }
  }
  // a name being typed is kept when the photo changes or the faces are hidden
  $effect(() => {
    const session = s
    return () => commitName(false, session)
  })

  const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const

  // labels under the boxes: numbers only when the faces are small on screen, and pushed down
  // where they would cover each other (the selected face's label is placed first, in full)
  let measureCtx: CanvasRenderingContext2D | null = null
  function textW(t: string) {
    measureCtx ??= document.createElement('canvas').getContext('2d')
    if (!measureCtx) return t.length * 7
    measureCtx.font = '600 12px ' + getComputedStyle(document.body).fontFamily
    return measureCtx.measureText(t).width
  }
  const labels = $derived.by(() => {
    const out = new Map<string, { x: number; y: number; full: boolean }>()
    if (!host) return out
    const widths = all.map((f) => (f.box as Box)[2] * W * zoom).sort((a, b) => a - b)
    const small = widths.length > 0 && widths[Math.floor(widths.length / 2)] < 44
    const placed: [number, number, number, number][] = []
    const LH = 20
    const first = [...all].sort((a, b) => Number(b.key === app.selectedFace || b.key === app.hoverFace) - Number(a.key === app.selectedFace || a.key === app.hoverFace))
    for (const f of first) {
      const b = boxOf(f)
      const full = !small || f.key === app.selectedFace || f.key === app.hoverFace || !f.name
      const n = order.indexOf(f) + 1
      const text = full ? (f.name ? `${n}  ${f.name}` : 'Who is this?') : String(n)
      const w = Math.min(220, textW(text) + 12)
      let x = full ? sx(b[0]) : sx(b[0] + b[2] / 2) - w / 2
      x = Math.max(2, Math.min((hostW || 9999) - w - 2, x))
      let y = sy(b[1] + b[3]) + 3
      for (let guard = 0; guard < 40; guard++) {
        const hit = placed.find((p) => x < p[0] + p[2] && x + w > p[0] && y < p[1] + p[3] && y + LH > p[1])
        if (!hit) break
        y = hit[1] + hit[3] + 2
      }
      placed.push([x, y, w, LH])
      out.set(f.key!, { x, y, full })
    }
    return out
  })
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="faces-layer" class:adding={app.faceTool === 'add' && !inert} class:inert bind:this={host} bind:clientWidth={hostW}
  onpointerdown={startNew} onpointermove={move} onpointerup={up} onpointercancel={() => { drag = null; live = null }}>
  {#each all as f (f.key)}
    {@const b = boxOf(f)}
    {@const n = order.indexOf(f) + 1}
    {@const sel = app.selectedFace === f.key}
    <div class="face" class:named={!!f.name} class:sel class:hover={app.hoverFace === f.key} data-face={f.key}
      role="button" tabindex={!inert && (sel || (!app.selectedFace && f === all[0])) ? 0 : -1}
      aria-label={f.name ? `Face ${n}: ${f.name}. Arrow keys move it, Alt+arrows resize, Enter renames, Delete removes.` : 'Face without a name. Enter to name it, Delete to remove it.'}
      style="left:{sx(b[0])}px;top:{sy(b[1])}px;width:{b[2] * W * zoom}px;height:{b[3] * H * zoom}px"
      onpointerdown={(e) => startBox(e, f, 'move')} onkeydown={(e) => keyBox(e, f)}
      onpointerenter={() => (app.hoverFace = f.key!)} onpointerleave={() => (app.hoverFace = null)}
      ondblclick={() => openName(f.key!)}>
      {#if sel && !inert}
        {#each HANDLES as h}
          <span class="hd {h}" onpointerdown={(e) => startBox(e, f, h)} aria-hidden="true"></span>
        {/each}
      {/if}
    </div>
    {@const lab = labels.get(f.key!)}
    {#if naming?.key !== f.key && lab}
      <button class="tag" class:unnamed={!f.name} class:sel class:num={!lab.full} tabindex="-1" style="left:{lab.x}px;top:{lab.y}px"
        onpointerdown={(e) => { e.stopPropagation(); if (naming && naming.key !== f.key) commitName() }} onclick={() => !inert && openName(f.key!)}
        onpointerenter={() => (app.hoverFace = f.key!)} onpointerleave={() => (app.hoverFace = null)}
        data-tip={inert ? undefined : f.name ? `${f.name}: click to rename` : 'Click to name this face'}>
        {#if !lab.full}<b>{n}</b>{:else if f.name}<b>{n}</b> {f.name}{:else}Who is this?{/if}
      </button>
    {/if}
  {/each}
  {#if live?.key === '__new'}
    <div class="face named sel" style="left:{sx(live.box[0])}px;top:{sy(live.box[1])}px;width:{live.box[2] * W * zoom}px;height:{live.box[3] * H * zoom}px"></div>
  {/if}
  {#if naming}
    {@const f = all.find((x) => x.key === naming!.key)}
    {#if f}
      {@const b = boxOf(f)}
      <div class="namer" style="left:{sx(b[0])}px;top:{sy(b[1] + b[3]) + 3}px" onpointerdown={(e) => e.stopPropagation()}>
        <input class="namein" bind:value={naming.text} placeholder="Name" aria-label="Name of this person" data-key={naming.key} onkeydown={keyName} oninput={() => (pick = -1)}
          onblur={(e) => { if (naming && naming.key === (e.currentTarget as HTMLInputElement).dataset.key) commitName() }}
          role="combobox" aria-expanded={suggestions.length > 0} aria-controls="face-sugg" aria-autocomplete="list" />
        {#if suggestions.length}
          <ul class="sugg" id="face-sugg" role="listbox">
            {#each suggestions as name, i}
              <li role="option" aria-selected={i === pick}><button tabindex="-1" class:on={i === pick} onpointerdown={(e) => { e.preventDefault(); if (naming) naming.text = name; commitName(true) }}>{name}</button></li>
            {/each}
          </ul>
        {/if}
        <span class="hint">Enter: save · Esc: cancel</span>
      </div>
    {/if}
  {/if}
  {#if app.faceTool === 'add' && !inert}
    <div class="addhint">{app.faceToolTarget ? `Drag a box around ${nameOf(app.faceToolTarget) || 'the face'}` : 'Drag a box around a face'} · Esc to stop</div>
  {/if}
</div>

<style>
  .faces-layer { position: absolute; inset: 0; pointer-events: none; z-index: 3; overflow: hidden; }
  .faces-layer.adding { pointer-events: auto; cursor: crosshair; }
  .face { position: absolute; box-sizing: border-box; border: 2px dashed rgba(255, 255, 255, 0.85); border-radius: 2px; pointer-events: auto; cursor: move; box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.35); }
  .face.named { border: 2px solid #ffd23f; }
  .face.hover { box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.35), 0 0 0 4px rgba(255, 210, 63, 0.35); }
  .face.sel { border-color: #22d3ee; box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.45), 0 0 0 4px rgba(34, 211, 238, 0.3); }
  .face:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .inert .face, .inert .tag { pointer-events: none; }
  .adding .face, .adding .tag { pointer-events: none; }
  .hd { position: absolute; width: 10px; height: 10px; background: #22d3ee; border: 1.5px solid #0b2a33; border-radius: 2px; pointer-events: auto; }
  .hd.nw { left: -6px; top: -6px; cursor: nwse-resize; }
  .hd.se { right: -6px; bottom: -6px; cursor: nwse-resize; }
  .hd.ne { right: -6px; top: -6px; cursor: nesw-resize; }
  .hd.sw { left: -6px; bottom: -6px; cursor: nesw-resize; }
  .hd.n { left: calc(50% - 5px); top: -6px; cursor: ns-resize; }
  .hd.s { left: calc(50% - 5px); bottom: -6px; cursor: ns-resize; }
  .hd.e { right: -6px; top: calc(50% - 5px); cursor: ew-resize; }
  .hd.w { left: -6px; top: calc(50% - 5px); cursor: ew-resize; }
  .tag { position: absolute; pointer-events: auto; border: 0; border-radius: 4px; padding: 2px 6px; font: 600 12px var(--font-ui); background: rgba(0, 0, 0, 0.72); color: #fff; cursor: text; white-space: nowrap; max-width: 220px; overflow: hidden; text-overflow: ellipsis; }
  .tag b { color: #ffd23f; margin-right: 3px; }
  .tag.num { padding: 2px 5px; }
  .tag.num b { margin: 0; }
  .tag.unnamed { background: rgba(255, 255, 255, 0.92); color: #1d1d1f; font-weight: 500; }
  .tag.sel { background: #0e7490; }
  .namer { position: absolute; pointer-events: auto; z-index: 5; display: flex; flex-direction: column; gap: 3px; min-width: 180px; }
  .namein { height: 28px; border-radius: var(--radius); border: 2px solid #22d3ee; padding: 0 8px; font: 13px var(--font-ui); background: var(--bg-2); color: var(--fg); box-shadow: var(--shadow); outline: none; }
  .sugg { list-style: none; margin: 0; padding: 3px; background: var(--bg-2); border: 1px solid var(--line-2); border-radius: var(--radius); box-shadow: var(--shadow); }
  .sugg button { display: block; width: 100%; text-align: left; border: 0; background: none; padding: 4px 8px; border-radius: 4px; font: 12.5px var(--font-ui); color: var(--fg); cursor: pointer; }
  .sugg button:hover, .sugg button.on { background: var(--bg-hover); }
  .hint { font-size: 10.5px; color: #fff; text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8); }
  .addhint { position: absolute; left: 50%; top: 10px; transform: translateX(-50%); background: rgba(20, 20, 22, 0.8); color: #fff; font-size: 12px; padding: 5px 12px; border-radius: 14px; pointer-events: none; }
</style>
