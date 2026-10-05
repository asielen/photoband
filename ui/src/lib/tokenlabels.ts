// Plain-language names for caption-format fields ("tokens"), with an example value each.
// Used by the caption format editor's autocomplete and its "Insert field" menu, so people
// pick "Date — June 14, 1952" instead of having to know {date:mmmm d, yyyy}.

export interface FieldChoice {
  /** what goes between the braces */
  insert: string
  /** what people would call it */
  label: string
  /** a typical value */
  example: string
  /** a one-line explanation (shown as a tooltip) */
  hint?: string
}

export const FIELD_CHOICES: FieldChoice[] = [
  { insert: 'title', label: 'Title', example: 'Picnic at Lake Merced', hint: 'The photo’s title from its photo information.' },
  { insert: 'caption', label: 'Description', example: 'Sunday picnic after church', hint: 'The photo’s description or caption from its photo information.' },
  { insert: 'notes', label: 'Notes', example: 'Taken at Grandma’s 80th birthday', hint: 'Free-text notes stored with the photo (its User Comment or Instructions), such as photokin’s analysis.' },
  { insert: 'date:mmmm d, yyyy', label: 'Date', example: 'June 14, 1952', hint: 'When the photo was taken (never the date it was scanned). Missing parts (like an unknown day) are left out; an approximate date such as “circa 1950” is written as it is. When parts of the date are marked as estimates (in Photoband or by photokin), the date gets “c.” (“c. July 1944”); unknown parts are left out.' },
  { insert: 'date:mmmm yyyy', label: 'Month and year', example: 'June 1952' },
  { insert: 'date:yyyy', label: 'Year', example: '1952' },
  { insert: 'date:yyyy-mm-dd', label: 'Date as numbers', example: '1952-06-14' },
  { insert: 'date', label: 'Date (as recorded)', example: 'June 1952', hint: 'The date written as precisely as the photo information allows.' },
  { insert: 'digitized:mmmm d, yyyy', label: 'Scan date', example: 'May 1, 2023', hint: 'When the photo was scanned or the file was made, not when the photo was taken.' },
  { insert: 'digitized:yyyy', label: 'Scan year', example: '2023' },
  { insert: 'names', label: 'People, left to right', example: 'Ann, Bea and Carl', hint: 'Names of the tagged faces, in order from left to right.' },
  { insert: 'names:rows', label: 'People, by row', example: 'Front row, L–R: Ann, Bea; Back row, L–R: Carl', hint: 'For group photos: names grouped by row, front to back.' },
  { insert: 'names.count', label: 'Number of people named', example: '3' },
  { insert: 'faces.unnamed_count', label: 'Number of unnamed faces', example: '2', hint: 'Faces that are marked but have no name, e.g. “and 2 unidentified”.' },
  { insert: 'location', label: 'Place', example: 'Lake Merced, San Francisco, California, USA', hint: 'Location, city, state and country joined with commas.' },
  { insert: 'city', label: 'City', example: 'San Francisco' },
  { insert: 'state', label: 'State or province', example: 'California' },
  { insert: 'country', label: 'Country', example: 'USA' },
  { insert: 'creator', label: 'Photographer', example: 'Robert Church' },
  { insert: 'keywords', label: 'Keywords', example: 'picnic, family, 1950s' },
  { insert: 'today:mmmm d, yyyy', label: 'Today’s date', example: 'October 1, 2026' },
  { insert: 'filename', label: 'File name', example: 'IMG_0042.tif' },
  { insert: 'stem', label: 'File name without extension', example: 'IMG_0042' },
  { insert: 'folder', label: 'Folder name', example: '1952 Picnic' },
]

/** Extra fields only some formats accept (e.g. the template name in file names). */
export const EXTRA_CHOICES: Record<string, FieldChoice> = {
  template: { insert: 'template', label: 'Template name', example: 'Classic Polaroid' },
}

/** The token name of an insert text: "date:mmmm d, yyyy" → "date", "names:rows" → "names". */
export function baseName(insert: string): string {
  return insert.split(/[:|]/)[0]
}

/** Plain label for a token name (as reported in "no value for" lists). */
export function fieldLabel(token: string): string {
  const name = baseName(token)
  if (EXTRA_CHOICES[name]) return EXTRA_CHOICES[name].label
  const exact = FIELD_CHOICES.find((c) => c.insert === name)
  if (exact) return name === 'date' ? 'Date' : exact.label
  return FIELD_CHOICES.find((c) => baseName(c.insert) === name)?.label ?? name
}

/**
 * Every choice for a format field: the plain-language list, then any token the server knows
 * that isn't covered (so nothing is ever unreachable), then the extras this field accepts.
 */
export function fieldChoices(serverTokens: { name: string; description?: string }[], extras: string[] = []): FieldChoice[] {
  const out = [...FIELD_CHOICES]
  const covered = new Set(out.map((c) => baseName(c.insert)))
  for (const t of serverTokens) {
    if (covered.has(t.name) || EXTRA_CHOICES[t.name]) continue
    out.push({ insert: t.name, label: t.name, example: '', hint: t.description })
    covered.add(t.name)
  }
  for (const x of extras) if (EXTRA_CHOICES[x]) out.push(EXTRA_CHOICES[x])
  return out
}

/** Choices matching what was typed after "{": the raw token text or any word of the label. */
export function filterChoices(choices: FieldChoice[], typed: string): FieldChoice[] {
  const q = typed.trim().toLowerCase()
  if (!q) return choices
  return choices.filter((c) => c.insert.toLowerCase().startsWith(q) || c.label.toLowerCase().split(/[\s,()’']+/).some((w) => w.startsWith(q)))
}
