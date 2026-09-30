// Live overview, shared by every page. Polls every 5s while the tab is visible.
export function useOverview() {
  const data = useState<Overview | null>('overview', () => null)
  const error = useState<string | null>('overview-error', () => null)
  const api = useApi()

  async function refresh() {
    try {
      const o = await api<Overview>('/api/overview')
      assignCables([...o.apps.map(a => a.name), ...o.observed.map(x => x.unit.replace('.service', '')),
        ...o.gpus.flatMap(g => g.users.map(u => u.name))])
      data.value = o
      error.value = null
    } catch (e) {
      error.value = apiError(e)
    }
  }

  onMounted(() => {
    refresh()
    const t = setInterval(() => { if (!document.hidden) refresh() }, 5000)
    const onVis = () => { if (!document.hidden) refresh() }
    document.addEventListener('visibilitychange', onVis)
    onBeforeUnmount(() => { clearInterval(t); document.removeEventListener('visibilitychange', onVis) })
  })
  return { data, error, refresh }
}
