import { describe, expect, it } from 'vitest'
import { relToPhoto } from '../lib/savepreview.svelte'

const win = (p: string) => p.replace(/\//g, String.fromCharCode(92)) // write Windows paths with / here

describe('relToPhoto', () => {
  it('shortens paths inside the photo folder', () => {
    expect(relToPhoto(win('D:/Scans/a.tif'), win('D:/Scans/_originals/a-original.tif'))).toBe(win('_originals/a-original.tif'))
    expect(relToPhoto('/u/scans/a.jpg', '/u/scans/a-captioned.jpg')).toBe('a-captioned.jpg')
  })
  it('keeps sibling folders that only share a prefix whole', () => {
    expect(relToPhoto(win('D:/Scans/a.tif'), win('D:/Scans2/a-captioned.tif'))).toBe(win('D:/Scans2/a-captioned.tif'))
    expect(relToPhoto(win('D:/Scans/a.tif'), win('D:/Scans-backup/a__1.tif'))).toBe(win('D:/Scans-backup/a__1.tif'))
    expect(relToPhoto('/u/Scans/a.jpg', '/u/Scans.bak/a.jpg')).toBe('/u/Scans.bak/a.jpg')
  })
})
