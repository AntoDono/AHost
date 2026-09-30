<script setup lang="ts">
const { data, error, refresh } = useOverview()
const api = useApi()
const toast = useToast()
const query = ref('')
const busy = ref<Record<string, string | null>>({})
const drawerOpen = ref(false)
const drawerTab = ref('overview')
const selected = ref<string | null>(null)
const confirmStop = ref<AppInfo | null>(null)

const apps = computed(() => {
  const q = query.value.toLowerCase().trim()
  const all = data.value?.apps ?? []
  const order: Record<string, number> = { failed: 0, unhealthy: 1, starting: 2, running: 3, stopped: 4 }
  return all
    .filter(a => !q || a.name.includes(q) || a.domains.some(d => d.includes(q)) || a.processes.some(p => String(p.port).includes(q)))
    .sort((a, b) => (order[a.state] ?? 9) - (order[b.state] ?? 9) || a.name.localeCompare(b.name))
})
const selectedApp = computed(() => data.value?.apps.find(a => a.name === selected.value) ?? null)
const trouble = computed(() => (data.value?.apps ?? []).filter(a => ['failed', 'unhealthy'].includes(a.state)))

const route = useRoute()
watch([() => route.query.app, () => data.value], () => {
  const q = route.query.app
  if (typeof q === 'string' && data.value?.apps.some(a => a.name === q) && selected.value !== q) open(q)
}, { immediate: true })

function open(name: string, tab = 'overview') {
  selected.value = name
  drawerTab.value = tab
  drawerOpen.value = true
}

function request(app: AppInfo, action: 'start' | 'stop' | 'restart') {
  if (action === 'stop') confirmStop.value = app
  else run(app.name, action)
}

const DONE: Record<string, string> = { start: 'Service started', stop: 'Service stopped', restart: 'Service restarted' }
async function run(name: string, action: 'start' | 'stop' | 'restart') {
  confirmStop.value = null
  busy.value[name] = action
  try {
    await api(`/api/apps/${name}/${action}`, { method: 'POST' })
    await refresh()
    toast.add({ title: `${DONE[action]}: ${name}`, color: 'success' })
  } catch (e) {
    toast.add({ title: `${action[0].toUpperCase() + action.slice(1)} failed: ${name}`, description: apiError(e), color: 'error' })
  } finally {
    busy.value[name] = null
  }
}
</script>

<template>
  <div class="max-w-[90rem] mx-auto space-y-6">
    <header class="flex flex-wrap items-end gap-4 justify-between">
      <div>
        <h1 class="silk text-3xl text-highlighted">Rack</h1>
        <p class="text-sm text-muted mt-1" v-if="data">
          {{ data.apps.filter(a => a.state === 'running').length }} of {{ data.apps.length }} apps running
          <template v-if="trouble.length"> · <span class="text-[var(--ah-fault)]">{{ trouble.map(a => a.name).join(', ') }} need attention</span></template>
        </p>
      </div>
      <div class="flex gap-2 w-full sm:w-auto">
        <UInput v-model="query" icon="i-lucide-search" placeholder="Find app, domain or port" class="grow sm:w-72" />
        <UButton to="/new" icon="i-lucide-plus" label="New hosting" />
      </div>
    </header>

    <UAlert v-if="error" color="error" variant="soft" icon="i-lucide-triangle-alert" title="Can't reach the AHost API" :description="error" />
    <UAlert v-for="(err, name) in data?.invalid ?? {}" :key="name" color="warning" variant="soft" icon="i-lucide-file-warning" :title="`${name}.toml can't be loaded`" :description="err" />

    <section aria-label="Apps" class="space-y-2">
      <div v-if="!data" class="space-y-2">
        <div v-for="i in 6" :key="i" class="h-[4.25rem] rounded-lg border border-default bg-[var(--ah-panel)] animate-pulse" />
      </div>
      <AppStrip v-for="a in apps" :key="a.name" :app="a" :gpus="data?.gpus ?? []" :busy="busy[a.name]" @open="(tab) => open(a.name, tab)" @action="(act) => request(a, act)" />
      <div v-if="data && !apps.length" class="rounded-lg border border-dashed border-default p-10 text-center text-muted">
        <template v-if="query">No app matches “{{ query }}”.</template>
        <template v-else>Nothing hosted yet. <NuxtLink to="/new" class="text-primary underline">Create your first hosting</NuxtLink>.</template>
      </div>
    </section>

    <section v-if="data?.observed.length" aria-label="Managed outside AHost">
      <h2 class="silk text-sm text-muted mb-2">Managed outside AHost</h2>
      <div class="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <UTooltip v-for="o in data.observed" :key="o.unit" text="Shown for reference. AHost doesn't manage this service.">
          <div class="flex items-center gap-3 rounded-lg border border-default bg-[var(--ah-panel)] px-3 py-2.5">
            <span class="led" :data-state="o.active === 'active' ? 'running' : o.active === 'failed' ? 'failed' : 'stopped'" />
            <span class="data truncate">{{ o.unit.replace('.service', '') }}</span>
            <span class="ml-auto data text-xs text-muted">{{ o.active === 'active' ? bytes(o.memory) : o.active }}</span>
          </div>
        </UTooltip>
      </div>
    </section>

    <AppDrawer v-model:open="drawerOpen" v-model:tab="drawerTab" :app="selectedApp" :busy="selected ? busy[selected] : null" @action="(n, act) => request(data!.apps.find(x => x.name === n)!, act)" @changed="refresh" />

    <UModal :open="!!confirmStop" :title="`Stop ${confirmStop?.name}?`" @update:open="(v) => { if (!v) confirmStop = null }">
      <template #body>
        <p class="text-sm">
          <template v-if="confirmStop?.domains.length"><span class="data">{{ confirmStop.domains.join(', ') }}</span> will stop responding. </template>
          It stays stopped after a reboot until you start it again.
        </p>
      </template>
      <template #footer>
        <div class="flex justify-end gap-2 w-full">
          <UButton color="neutral" variant="ghost" label="Cancel" @click="confirmStop = null" />
          <UButton color="error" icon="i-lucide-square" label="Stop service" @click="run(confirmStop!.name, 'stop')" />
        </div>
      </template>
    </UModal>
  </div>
</template>
