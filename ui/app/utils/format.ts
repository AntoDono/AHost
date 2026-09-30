export function bytes(n?: number | null): string {
  if (n == null) return '–'
  const u = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v >= 10 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`
}

export function mib(n?: number | null): string {
  return n == null ? '–' : n >= 1024 ? `${(n / 1024).toFixed(1)} GB` : `${n} MB`
}

export function since(ts?: string | null): string {
  if (!ts) return '–'
  const t = Date.parse(ts.replace(/ [A-Z]{2,5}$/, ''))
  if (Number.isNaN(t)) return ts
  return ago(Math.floor((Date.now() - t) / 1000))
}

export function ago(s: number): string {
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
}

// Patch-cable colors. Every app/service on the page gets its own, assigned in name order (see useOverview),
// so neighbours in a GPU bar never share a color. Hash fallback for names the overview doesn't know yet.
const CABLES = ['#2d6bd8', '#e8a400', '#1f9e8f', '#8a5cd0', '#d0433f', '#3b9b3f', '#d9731f', '#2a8fc9',
  '#c2448f', '#7a8c2a', '#5a7184', '#b0772c', '#4e5bd6', '#199bb0', '#9c4f9e', '#6d7b3a']
const assigned = new Map<string, string>()
export function assignCables(names: string[]) {
  assigned.clear()
  ;[...new Set(names)].sort().forEach((n, i) => assigned.set(n, CABLES[i % CABLES.length]))
}
export function cableColor(name: string): string {
  const a = assigned.get(name)
  if (a) return a
  let h = 0
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return CABLES[h % CABLES.length]
}

export const STATE_LABEL: Record<string, string> = {
  running: 'Running', unhealthy: 'Not responding', starting: 'Starting', failed: 'Failed', stopped: 'Stopped',
}
