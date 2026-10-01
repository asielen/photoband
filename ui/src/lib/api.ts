// Thin client for the local backend. The per-launch token arrives in the page URL
// (#t=... after the launcher's one-time /?b= hand-off, or ?t=...) and is sent with every request.

const params = new URLSearchParams(location.search)
const hashParams = new URLSearchParams(location.hash.slice(1))
function readToken(): string {
  const t = hashParams.get('t') || params.get('t')
  if (t) {
    try { sessionStorage.setItem('photoband-token', t) } catch { /* private mode */ }
    // keep the token out of the visible URL/history
    history.replaceState(null, '', location.pathname)
    return t
  }
  try { return sessionStorage.getItem('photoband-token') || '' } catch { return '' }
}
export const TOKEN: string = readToken()

export class ApiError extends Error {
  status: number
  data: any
  constructor(message: string, status: number, data?: any) {
    super(message)
    this.status = status
    this.data = data
  }
}

async function handle(r: Response) {
  const ct = r.headers.get('content-type') || ''
  const data = ct.includes('application/json') ? await r.json() : await r.text()
  if (!r.ok) {
    const msg = (data && (data.error || data.detail)) || r.statusText || 'Request failed'
    throw new ApiError(typeof msg === 'string' ? msg : JSON.stringify(msg), r.status, data)
  }
  return data
}

export async function get<T = any>(path: string, query: Record<string, any> = {}): Promise<T> {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null) q.set(k, String(v))
  const qs = q.toString()
  const r = await fetch(path + (qs ? '?' + qs : ''), { headers: { 'X-Photoband-Token': TOKEN } })
  return handle(r)
}

export async function post<T = any>(path: string, body: any = {}, opts: { keepalive?: boolean } = {}): Promise<T> {
  const init: RequestInit = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Photoband-Token': TOKEN },
    body: JSON.stringify(body),
  }
  let r: Response
  if (opts.keepalive) {
    // keepalive lets the request finish while the page closes; browsers cap its body (~64 KB)
    try {
      r = await fetch(path, { ...init, keepalive: true })
    } catch {
      r = await fetch(path, init)
    }
  } else {
    r = await fetch(path, init)
  }
  return handle(r)
}

export async function del<T = any>(path: string): Promise<T> {
  const r = await fetch(path, { method: 'DELETE', headers: { 'X-Photoband-Token': TOKEN } })
  return handle(r)
}

export async function postForm<T = any>(path: string, form: FormData): Promise<T> {
  const r = await fetch(path, { method: 'POST', headers: { 'X-Photoband-Token': TOKEN }, body: form })
  return handle(r)
}

/** URL for <img>/Image() loads (headers can't be set there, so the token rides in the query). */
export function url(path: string, query: Record<string, any> = {}): string {
  const q = new URLSearchParams({ ...Object.fromEntries(Object.entries(query).map(([k, v]) => [k, String(v)])), t: TOKEN })
  return `${path}?${q.toString()}`
}

export function proxyUrl(path: string, version = 0, thumb = false) {
  return url('/api/photo/proxy', { path, thumb: thumb ? 1 : 0, v: version })
}

export async function download(path: string, filename: string) {
  const r = await fetch(path, { headers: { 'X-Photoband-Token': TOKEN } })
  if (!r.ok) throw new ApiError('Download failed', r.status)
  const blob = await r.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = filename
  document.body.appendChild(a)
  a.click()
  setTimeout(() => {
    URL.revokeObjectURL(a.href)
    a.remove()
  }, 1000)
}
