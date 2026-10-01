// Shared data shapes between the UI and the Python backend.

export type Align = 'left' | 'center' | 'right' | 'justify'
export type CaseMode = 'none' | 'upper' | 'lower' | 'smallcaps'
export type ScaleMode = 'relative' | 'physical'
export type Rect = [number, number, number, number] // x, y, w, h

export interface TextStyle {
  font: string // font family id
  weight: number
  italic: boolean
  size: number // relative: % of photo width; physical: pt
  color: string
  align: Align
  lineHeight: number // multiplier
  letterSpacing: number // % of font size (-5..30)
  spaceBefore: number // relative: % of photo width; physical: mm
  spaceAfter: number
  case: CaseMode
}

export interface Block {
  id: string
  name: string
  format: string
  column: number
  style: TextStyle
}

export interface LayoutSettings {
  border: { top: number; right: number; bottom: number; left: number }
  lockSides: boolean
  borderColor: string
  bandColor: string
  linkColors: boolean
  bandHeight: { mode: 'auto' | 'fixed'; min: number; overflow: 'warn' | 'shrink' }
  padding: { top: number; right: number; bottom: number; left: number }
  textMaxWidth: number // % of band width
  vAlign: 'top' | 'middle' | 'bottom'
  columns: { count: 1 | 2; split: number; gutter: number }
  divider: { enabled: boolean; width: number; color: string; inset: number }
  keyline: { enabled: boolean; width: number; color: string }
}

export interface Template {
  id: string
  name: string
  builtin?: boolean
  description?: string
  scaleMode: ScaleMode
  layout: LayoutSettings
  blocks: Block[]
  schema?: number
  /** set on a template that came embedded in a photo's record and isn't installed: its original id and name */
  fromFile?: { id: string; name: string; hash: string }
}

export interface FontFaceInfo {
  file: string
  weight: number
  weightRange: [number, number] | null
  italic: boolean
  subfamily: string
  smallCaps?: boolean
}

export interface FontFamily {
  id: string
  family: string
  source: 'bundled' | 'system' | 'user' | 'google'
  role: string
  license: string
  fallback: boolean
  faces: FontFaceInfo[]
  coverage: [number, number][]
  smallCaps: boolean
}

export interface Issue {
  kind: string
  start: number
  end: number
  message: string
}

export interface Resolution {
  text: string
  plain: string
  empty: boolean
  tokens_used: string[]
  empty_tokens: string[]
  issues: Issue[]
}

export interface Face {
  name: string
  box: [number, number, number, number] | null
  source: string
}

export interface ImageInfo {
  path: string
  format: 'TIFF' | 'JPEG' | 'PNG'
  width: number
  height: number
  upright_width: number
  upright_height: number
  channels: number
  dtype: string
  bits: number
  mode: string
  compression: string
  icc: boolean
  dpi: [number, number] | null
  orientation: number
  pages: number
  save_blocked: string | null
  notes: string[]
  size_bytes: number
}

export interface PhotoMeta {
  path: string
  name: string
  info: ImageInfo
  fields: Record<string, any>
  fieldSources: Record<string, string>
  faces: { named: Face[]; unnamed: Face[]; unnamed_count: number; has_positions: boolean; warnings: string[] }
  warnings: string[]
  raw: [string, string][]
  hasRecord: boolean
  stat: [number, string, string?]
  draft: PhotoDraft | null
}

export interface TextLineInfo {
  box: Rect
  text: string
  confidence: number
  words: { text: string; confidence: number; box: Rect }[]
}

export interface BandInfo {
  found: boolean
  photo_rect: Rect
  bands: { side: string; rect: Rect; color?: number[]
  angle?: number
  photo_quad?: [number, number][]
  edge_blur?: number
  color_drift?: number
  nested?: boolean
}[]
  band_color: number[]
  band_color_hex: string
  textured: boolean
  noise: number
  confidence: number
}

export interface ExistingAnalysis {
  case: 'A' | 'B' | 'C' | 'D' | null
  source: 'record' | 'marker+payload' | 'marker' | 'detection' | null
  confidence?: number
  sourceRect?: Rect
  /** a captioned copy Photoband saved (not an original captioned in place) */
  isCopy?: boolean
  state?: { template: Template | null; templateId: string | null; overrides: Overrides | null; blocks: BlockState[] | null }
  originalText?: any
  band?: BandInfo
  blocks?: { box: Rect; lines: TextLineInfo[]; role?: 'caption' | 'other' }[]
  styles?: ({ font_size_px: number; cap_height_px: number; align: string; color_hex: string; line_height: number } | null)[]
  hints?: string[]
  scanCues?: Record<string, any>
  scanScore?: number
  text?: string
  style?: { font_size_px: number; cap_height_px: number; align: string; color_hex: string; line_height: number } | null
  textOverPhoto?: Rect[]
  warnings: string[]
  marker?: { photoRect: Rect; payload: boolean }
  lossyRecaption?: boolean
  hasText?: boolean
  record?: any
}

export interface BlockState {
  id: string
  custom: boolean
  text: string // markup; for linked blocks, the last resolved text
}

export interface Overrides {
  layout?: Partial<Record<string, any>>
  blocks?: Record<string, Partial<TextStyle>>
  scaleMode?: ScaleMode
}

export type EditMode = 'band' | 'rebuild' | 'erase'

export interface PhotoDraft {
  templateId: string
  overrides: Overrides
  blocks: Record<string, BlockState>
  mode: EditMode
  sourceRect: Rect | null
  photoRect: Rect | null // confirmed photo edge for existing text (erase)
  existingChoice?: 'recognized' | 'template' | null
  brushAdd?: string | null
  brushRemove?: string | null
  /** case A: the user chose "Add a new band" (keep the old Photoband band as part of the photo) */
  keepBand?: boolean
}

export interface Run {
  block: string
  text: string
  x: number
  y: number // baseline
  w: number
  font: string // CSS font string at output scale without size (see render)
  family: string // CSS family stack
  weight: number
  italic: boolean
  size: number
  color: string
  letterSpacing: number
  smallCaps: boolean
  area: string
}

export interface LayoutResult {
  version: 1
  mode: 'band' | 'erase'
  sourceRect: Rect
  canvas: [number, number]
  photoRect: Rect
  fills: { rect: Rect; color: string }[]
  bandColor: string
  protect: Rect[]
  textAreas: { id: string; rect: Rect }[]
  runs: Run[]
  textColors: string[]
  blockBoxes: Record<string, Rect>
  warnings: Warning[]
  scale: { pxPerUnit: number; dpi: number | null; unit: string }
  shrink: number
}

export interface Warning {
  kind: 'overflow' | 'glyph' | 'small' | 'metadata' | 'font' | 'dpi' | 'color' | 'info'
  message: string
  block?: string
  /** stable id for warnings other code reacts to (the message is for people and may change) */
  code?: 'names-missing' | 'names-order' | 'names-regions'
}

export interface Settings {
  general: { displayUnit: '%' | 'px' | 'pt' | 'mm'; theme: 'system' | 'light' | 'dark'; defaultTemplate: string; showTooltips: boolean }
  saving: {
    location: 'subfolder' | 'fixed' | 'same'
    subfolderName: string
    fixedFolder: string
    fileName: string
    onExists: 'increment' | 'overwrite' | 'ask'
    outputFormat: 'same' | 'tiff' | 'jpeg' | 'png'
    jpegQuality: number
    keepFileDates: boolean
    backupOriginals: boolean
    backupFolder: string
    embedMarker: boolean
    allowMultipageSave: boolean
    allowOverwriteHandwritten: boolean
  }
  advanced: { cacheSizeMB: number; exiftoolPath: string }
  batch: {
    includeSubfolders: boolean
    saveMode: 'copy' | 'overwrite'
    which: 'all' | 'selected' | 'uncaptioned'
    caseA: 'recaption' | 'skip'
    caseB: 'rebuild' | 'skip'
    caseC: 'skip' | 'erase'
    useDrafts: boolean
    warnings: 'include' | 'hold'
  }
  session: { lastTemplate: string; lastFolder: string; window: { w: number; h: number }; jpegWarned: boolean }
}

export interface SaveResult {
  ok: boolean
  path: string
  out_path: string
  backup_path: string
  notes: string[]
  marker: { robust?: boolean; payload?: boolean; reason?: string }
  size: [number, number]
  format: string
  elapsed_ms: number
  error: string
  code: string
}
