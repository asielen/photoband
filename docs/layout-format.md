# Layout object

This is the contract between the UI layout engine (`ui/src/lib/layout.ts`) and the backend compositor (`photoband/composite.py`). All coordinates are in **output pixels**, in the upright orientation.

| Key | Meaning |
|---|---|
| `version` | Always `1`. |
| `mode` | `"band"` means a new canvas with borders. `"erase"` means the source canvas is kept, the old text is erased and the new text is drawn in the band. |
| `sourceRect` `[x,y,w,h]` | The part of the upright source that is the photo: the whole image, or a crop back to the photo for re-captions and rebuilds. |
| `canvas` `[W,H]` | The output size. |
| `photoRect` `[x,y,w,h]` | Where the photo pixels go, byte for byte. In erase mode this is the detected photo, which is never touched. |
| `fills[]` | `{rect, color}` rectangles for borders, the band, the divider and the keyline, painted in order before the photo is copied in. |
| `bandColor` | sRGB hex. The backend converts it to the file's ICC space, bit depth and channels. |
| `protect[]` | Rectangles (divider, keyline) that the hidden marker must not touch. |
| `textAreas[]` | `{id, rect}` per column. |
| `runs[]` | Positioned text: `{block, text, x, y(baseline), w, family, weight, italic, size, color, letterSpacing, smallCaps, area}`. |
| `textColors[]` | The distinct text colours. The backend maps each anti-aliased tile pixel to the nearest of these and composites that colour converted into the file's space. |

The UI renders `runs` at scale 1 into transparent PNG tiles no larger than 4096 px and posts them with the layout. The backend never renders text.
