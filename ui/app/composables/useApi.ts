// All API calls: same origin, session cookie, and the X-AHost header the server requires on changes.
export function useApi() {
  return $fetch.create({
    headers: { 'X-AHost': '1' },
    credentials: 'same-origin',
    onResponseError({ response }) {
      if (response.status === 401 && !window.location.pathname.startsWith('/login')) {
        useState<string | null | undefined>('user').value = null
        navigateTo('/login')
      }
    },
  })
}

export function apiError(e: unknown): string {
  const d = (e as { data?: { detail?: unknown, error?: string } })?.data
  if (typeof d?.detail === 'string') return d.detail
  if (d?.error) return d.error
  if (Array.isArray(d?.detail)) return d.detail.map((x: { msg?: string }) => x.msg).join('; ')
  return (e as Error)?.message || 'Request failed'
}
