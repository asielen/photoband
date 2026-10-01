// The Details panel's Date and Scan date rows: the date as a caption prints it, with the
// raw metadata value after it when that differs.

/**
 * `rendered` is what {date} / {digitized} prints ("" while it loads or when the value
 * names no date); `raw` is the file's own value. A raw value with no digit 1-9
 * ("0000:00:00 00:00:00", "    :  :  ") is a placeholder, not a date: the row is empty
 * (shown as "—") rather than repeating the placeholder.
 */
export function dateRowText(rendered: string | undefined, raw: string | undefined): string {
  const r = (raw ?? '').trim()
  if (!/[1-9]/.test(r)) return ''
  if (rendered && rendered !== raw) return `${rendered} (${raw})`
  return raw ?? ''
}
