<script setup lang="ts">
interface PortsView {
  range: [number, number], reserved: number[]
  assigned: { port: number, app: string, process: string, pinned: boolean, listening: boolean }[]
  other: { port: number, who: string, in_range: boolean, reserved: boolean }[]
}
const api = useApi()
const view = ref<PortsView | null>(null)
const hover = ref<number | null>(null)
onMounted(async () => { view.value = await api<PortsView>('/api/ports') })

const byPort = computed(() => {
  const m = new Map<number, { label: string, kind: 'app' | 'other', color: string }>()
  for (const a of view.value?.assigned ?? []) m.set(a.port, { label: `${a.app}${a.process !== 'main' ? ':' + a.process : ''}`, kind: 'app', color: cableColor(a.app) })
  for (const o of view.value?.other ?? []) if (!m.has(o.port)) m.set(o.port, { label: o.who || 'unknown', kind: 'other', color: 'var(--ah-dim)' })
  return m
})
const cells = computed(() => {
  if (!view.value) return []
  const [lo, hi] = view.value.range
  return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i)
})
const rangeUsed = computed(() => cells.value.filter(p => byPort.value.has(p)).length)
const rows = computed(() => {
  const all = [...(view.value?.assigned ?? []).map(a => ({ port: a.port, who: `${a.app}${a.process !== 'main' ? ':' + a.process : ''}`, kind: a.pinned ? 'AHost (pinned)' : 'AHost', up: a.listening, app: a.app })),
    ...(view.value?.other ?? []).map(o => ({ port: o.port, who: o.who || '–', kind: o.reserved ? 'reserved' : 'not AHost', up: true, app: '' }))]
  return all.sort((a, b) => a.port - b.port)
})
</script>

<template>
  <div class="max-w-6xl mx-auto space-y-6">
    <header>
      <h1 class="silk text-3xl text-highlighted">Ports</h1>
      <p class="text-sm text-muted mt-1" v-if="view">New apps get ports from {{ view.range[0] }}–{{ view.range[1] }} ({{ rangeUsed }} in use). Adopted apps keep the ports they already had.</p>
    </header>

    <section v-if="view" class="rounded-lg border border-default bg-[var(--ah-panel)] p-4" aria-label="Port range patch panel">
      <div class="flex justify-between mb-2 text-xs text-muted"><span class="silk">Patch panel {{ view.range[0] }}–{{ view.range[1] }}</span><span class="data text-xs">{{ hover ? `:${hover} ${byPort.get(hover)?.label ?? 'free'}` : 'hover a jack' }}</span></div>
      <div class="grid gap-[3px]" style="grid-template-columns: repeat(50, minmax(0, 1fr));">
        <span
          v-for="p in cells" :key="p"
          class="aspect-square rounded-[2px] border"
          :style="byPort.get(p) ? { background: byPort.get(p)!.color, borderColor: byPort.get(p)!.color } : { borderColor: 'var(--ah-rule)' }"
          :class="hover === p ? 'ring-2 ring-primary' : ''"
          @mouseenter="hover = p" @mouseleave="hover = null"
        />
      </div>
    </section>

    <section v-if="view" class="overflow-x-auto rounded-lg border border-default bg-[var(--ah-panel)]">
      <table class="w-full text-sm">
        <thead class="text-xs text-muted bg-[var(--ah-panel-2)]">
          <tr><th class="text-left px-4 py-2 font-medium">Port</th><th class="text-left px-4 py-2 font-medium">Used by</th><th class="text-left px-4 py-2 font-medium">Kind</th><th class="text-left px-4 py-2 font-medium">Listening</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.port + r.who" class="border-t border-default" :class="hover === r.port ? 'bg-[var(--ah-panel-2)]' : ''" @mouseenter="hover = r.port" @mouseleave="hover = null">
            <td class="px-4 py-2 data">
              <span class="inline-block size-2.5 rounded-sm mr-2 align-middle" :style="{ background: r.app ? cableColor(r.app) : 'var(--ah-dim)' }" />{{ r.port }}
            </td>
            <td class="px-4 py-2">{{ r.who }}</td>
            <td class="px-4 py-2 text-muted">{{ r.kind }}</td>
            <td class="px-4 py-2">{{ r.up ? 'yes' : 'no' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
