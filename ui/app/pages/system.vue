<script setup lang="ts">
interface Cpu { id: number, core: number, package: number, pct: number }
interface SystemView {
  model: string, cpus: Cpu[], cores: number, load: number[], uptime_s: number
  mem_total: number, mem_available: number, swap_total: number, swap_free: number
  disk_total: number, disk_used: number, gpus: Gpu[]
}
interface Proc { pid: number, name: string, cmd: string, user: string, pct: number, rss: number, unit: string, app: string | null }

const api = useApi()
const { data: overview } = useOverview()
const sys = ref<SystemView | null>(null)
const selected = ref<number | null>(null)
const procs = ref<Proc[] | null>(null)
const procsLoading = ref(false)
const showKernel = ref(false)
const shownProcs = computed(() => (procs.value ?? []).filter(p => showKernel.value || p.unit))
const hiddenKernel = computed(() => (procs.value ?? []).filter(p => !p.unit).length)
const error = ref('')

async function load() {
  try {
    sys.value = await api<SystemView>('/api/system')
    error.value = ''
  } catch (e) { error.value = apiError(e) }
}
async function loadProcs() {
  if (selected.value == null) return
  procsLoading.value = true
  try { procs.value = (await api<{ procs: Proc[] }>(`/api/system/cpu/${selected.value}`)).procs } finally { procsLoading.value = false }
}
function select(id: number) {
  if (selected.value === id) { selected.value = null; procs.value = null; return }
  selected.value = id
  procs.value = null
  loadProcs()
}
onMounted(() => {
  load()
  const t = setInterval(() => { if (!document.hidden) { load(); loadProcs() } }, 2500)
  onBeforeUnmount(() => clearInterval(t))
})

// 16 core columns, one cell per hardware thread (threads of a core share a column)
const columns = computed(() => {
  const byCore = new Map<string, Cpu[]>()
  for (const c of sys.value?.cpus ?? []) {
    const k = `${c.package}:${c.core}`
    byCore.set(k, [...(byCore.get(k) ?? []), c].sort((a, b) => a.id - b.id))
  }
  return [...byCore.values()].sort((a, b) => a[0].package - b[0].package || a[0].core - b[0].core)
})
const avg = computed(() => {
  const c = sys.value?.cpus ?? []
  return c.length ? Math.round(c.reduce((s, x) => s + x.pct, 0) / c.length) : 0
})
// load heat: steel -> cable blue -> signal amber -> fault red
function heat(p: number) {
  if (p < 3) return 'var(--ah-panel-2)'
  if (p < 40) return `color-mix(in srgb, var(--ah-cable) ${20 + p * 1.5}%, var(--ah-panel))`
  if (p < 75) return `color-mix(in srgb, var(--ah-signal) ${40 + (p - 40) * 1.5}%, var(--ah-panel))`
  return `color-mix(in srgb, var(--ah-fault) ${55 + (p - 75) * 1.8}%, var(--ah-signal))`
}
const memPct = computed(() => sys.value ? Math.round(100 * (1 - sys.value.mem_available / sys.value.mem_total)) : 0)
const diskPct = computed(() => sys.value ? Math.round(100 * sys.value.disk_used / sys.value.disk_total) : 0)
const appNames = computed(() => (overview.value?.apps ?? []).map(a => a.name))
</script>

<template>
  <div class="max-w-6xl mx-auto space-y-6">
    <header>
      <h1 class="silk text-3xl text-highlighted">System</h1>
      <p v-if="sys" class="text-sm text-muted mt-1">{{ sys.model }} · {{ sys.cores }} cores / {{ sys.cpus.length }} threads · up {{ ago(sys.uptime_s) }}</p>
    </header>
    <UAlert v-if="error" color="error" variant="soft" icon="i-lucide-triangle-alert" :description="error" />

    <!-- vitals -->
    <section v-if="sys" class="grid gap-3 sm:grid-cols-4">
      <div class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
        <div class="silk text-xs text-muted">CPU</div>
        <div class="font-mono text-2xl text-highlighted mt-1">{{ avg }}%</div>
        <div class="data text-xs text-muted">load {{ sys.load.join(' · ') }}</div>
      </div>
      <div class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
        <div class="silk text-xs text-muted">Memory</div>
        <div class="font-mono text-2xl text-highlighted mt-1">{{ memPct }}%</div>
        <div class="h-1.5 mt-2 rounded bg-[var(--ah-rule)] overflow-hidden"><div class="h-full bg-primary" :style="{ width: `${memPct}%` }" /></div>
        <div class="data text-xs text-muted mt-1">{{ bytes(sys.mem_total - sys.mem_available) }} / {{ bytes(sys.mem_total) }}</div>
      </div>
      <div class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
        <div class="silk text-xs text-muted">Swap</div>
        <div class="font-mono text-2xl text-highlighted mt-1">{{ sys.swap_total ? Math.round(100 * (1 - sys.swap_free / sys.swap_total)) : 0 }}%</div>
        <div class="data text-xs text-muted mt-3">{{ bytes(sys.swap_total - sys.swap_free) }} / {{ bytes(sys.swap_total) }}</div>
      </div>
      <div class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
        <div class="silk text-xs text-muted">Disk /</div>
        <div class="font-mono text-2xl text-highlighted mt-1">{{ diskPct }}%</div>
        <div class="h-1.5 mt-2 rounded bg-[var(--ah-rule)] overflow-hidden"><div class="h-full" :class="diskPct > 90 ? 'bg-[var(--ah-fault)]' : 'bg-primary'" :style="{ width: `${diskPct}%` }" /></div>
        <div class="data text-xs text-muted mt-1">{{ bytes(sys.disk_used) }} / {{ bytes(sys.disk_total) }}</div>
      </div>
    </section>

    <!-- CPU patch panel -->
    <section v-if="sys" class="rounded-lg border border-default bg-[var(--ah-panel)] p-4" aria-label="CPU threads">
      <div class="flex justify-between mb-3 text-xs text-muted">
        <span class="silk">CPU: one column per core, one cell per thread</span>
        <span>Click a cell to see what runs on it</span>
      </div>
      <div class="grid gap-1.5" :style="{ gridTemplateColumns: `repeat(${columns.length}, minmax(0, 1fr))` }">
        <div v-for="col in columns" :key="`${col[0].package}:${col[0].core}`" class="flex flex-col gap-1.5">
          <button
            v-for="c in col" :key="c.id" type="button"
            class="aspect-[4/5] rounded-md border flex flex-col items-center justify-center transition-colors duration-300 cursor-pointer focus-visible:outline-2 focus-visible:outline-primary"
            :class="selected === c.id ? 'border-primary ring-2 ring-primary/50' : 'border-default hover:border-[var(--ah-muted)]'"
            :style="{ background: heat(c.pct) }"
            :aria-pressed="selected === c.id"
            :aria-label="`CPU ${c.id}: ${Math.round(c.pct)}% busy`"
            @click="select(c.id)"
          >
            <span class="data text-[0.8rem] font-medium" :class="c.pct >= 60 ? 'text-white' : 'text-highlighted'">{{ Math.round(c.pct) }}</span>
            <span class="data text-[0.62rem]" :class="c.pct >= 60 ? 'text-white/80' : 'text-dimmed'">cpu{{ c.id }}</span>
          </button>
          <span class="data text-[0.62rem] text-dimmed text-center">core {{ col[0].core }}</span>
        </div>
      </div>
      <div class="flex items-center gap-3 mt-3 text-xs text-muted">
        <span>Idle</span>
        <span class="h-2 w-40 rounded" style="background: linear-gradient(90deg, var(--ah-panel-2), var(--ah-cable), var(--ah-signal), var(--ah-fault))" />
        <span>Busy</span>
      </div>

      <!-- processes on the selected thread -->
      <div v-if="selected != null" class="mt-5 border-t border-default pt-4">
        <div class="flex items-center justify-between mb-2">
          <h2 class="font-medium text-highlighted">Running on cpu{{ selected }}</h2>
          <div class="flex items-center gap-4">
            <USwitch v-model="showKernel" size="sm" :label="`Kernel threads (${hiddenKernel})`" />
            <span class="text-xs text-muted hidden sm:inline">Last CPU each process ran on · refreshes every few seconds</span>
          </div>
        </div>
        <div v-if="!procs" class="text-sm text-muted">Sampling…</div>
        <div v-else class="overflow-x-auto rounded-md border border-default">
          <table class="w-full text-sm">
            <thead class="bg-[var(--ah-panel-2)] text-muted text-xs">
              <tr><th class="text-left px-3 py-2 font-medium">Process</th><th class="text-left px-3 py-2 font-medium">App / service</th><th class="text-right px-3 py-2 font-medium">CPU</th><th class="text-right px-3 py-2 font-medium">Memory</th><th class="text-left px-3 py-2 font-medium">User</th><th class="text-left px-3 py-2 font-medium">PID</th></tr>
            </thead>
            <tbody>
              <tr v-for="p in shownProcs" :key="p.pid" class="border-t border-default">
                <td class="px-3 py-1.5"><span class="font-medium">{{ p.name }}</span><div class="data text-xs text-muted truncate max-w-[26rem]" :title="p.cmd">{{ p.cmd }}</div></td>
                <td class="px-3 py-1.5">
                  <NuxtLink v-if="p.app" :to="{ path: '/rack', query: { app: p.app } }" class="inline-flex items-center gap-1.5 hover:underline">
                    <span class="size-2.5 rounded-sm" :style="{ background: cableColor(p.app) }" />{{ p.app }}
                  </NuxtLink>
                  <span v-else class="data text-xs text-muted">{{ p.unit ? p.unit.replace('.service', '') : 'kernel' }}</span>
                </td>
                <td class="px-3 py-1.5 data text-right" :class="p.pct >= 50 ? 'text-[var(--ah-fault)]' : ''">{{ p.pct.toFixed(1) }}%</td>
                <td class="px-3 py-1.5 data text-right">{{ bytes(p.rss) }}</td>
                <td class="px-3 py-1.5">{{ p.user }}</td>
                <td class="px-3 py-1.5 data">{{ p.pid }}</td>
              </tr>
              <tr v-if="!shownProcs.length"><td colspan="6" class="px-3 py-3 text-muted">{{ procs.length ? 'Only kernel threads on this CPU right now.' : 'Nothing is scheduled on this thread right now.' }}</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- GPUs -->
    <section v-if="sys?.gpus.length" aria-label="GPUs">
      <h2 class="silk text-sm text-muted mb-2">GPUs</h2>
      <GpuPanel :gpus="sys.gpus" :apps="appNames" />
    </section>
  </div>
</template>
