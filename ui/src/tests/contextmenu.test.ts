// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

vi.mock('../lib/api', () => ({ post: vi.fn(async () => ({ ok: true })) }))

describe('fileMenu', () => {
  it('opens from the keyboard as well as the mouse, and plain rows become focusable', async () => {
    const { closeContextMenu, contextMenu, fileMenu } = await import('../lib/contextmenu.svelte')
    const row = document.createElement('div')
    document.body.append(row)
    const act = fileMenu(row, { label: 'Actions for a.tif', items: () => [{ label: 'Show', run: () => {} }] })
    expect(row.tabIndex).toBe(0)
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }))
    expect(contextMenu!.items.length).toBe(1)
    expect(contextMenu!.label).toBe('Actions for a.tif')
    closeContextMenu()
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'F10', shiftKey: true, bubbles: true }))
    expect(contextMenu!.items.length).toBe(1)
    closeContextMenu()
    row.dispatchEvent(new MouseEvent('contextmenu', { clientX: 10, clientY: 20, bubbles: true, cancelable: true }))
    expect(contextMenu!.items.length).toBe(1)
    closeContextMenu()
    act.destroy()
  })

  it('a row holding a focusable control is not made a second tab stop; the key comes from the control', async () => {
    const { closeContextMenu, contextMenu, fileMenu } = await import('../lib/contextmenu.svelte')
    const label = document.createElement('label')
    const box = document.createElement('input')
    box.type = 'checkbox'
    label.append(box)
    document.body.append(label)
    fileMenu(label, { label: 'Actions for b.tif', items: () => [{ label: 'Show', run: () => {} }] })
    expect(label.hasAttribute('tabindex')).toBe(false)
    box.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }))
    expect(contextMenu!.label).toBe('Actions for b.tif')
    closeContextMenu()
  })
})
