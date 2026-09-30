<script setup lang="ts">
interface Detect { venvs: string[], uv: boolean, node: { start?: string, name?: string } | null, conda: boolean | null, hints: { kind: string, command: string }[] }
interface Preview { ok: boolean, errors: string[], warnings?: string[], steps?: string[], ports?: Record<string, number>, text?: string, toml: string }

const api = useApi()
const toast = useToast()
const { data: overview } = useOverview()

const f = reactive({
  name: '', workdir: '', kind: 'app' as 'app' | 'static' | 'both',
  runtime: 'plain' as 'plain' | 'venv' | 'uv' | 'node' | 'conda', venv: '.venv', node: '22', conda: '',
  command: '', portMode: 'auto' as 'auto' | 'fixed', port: 10000,
  address: 'domain' as 'domain' | 'router' | 'none',
  domains: [''] as string[],
  router: '', routerPath: '', newPath: '', strip: true,
  websocket: true, streaming: true, timeout: '', maxBody: '',
  statics: [] as { path: string, dir: string, spa: boolean }[],
  gpus: [] as string[],
  env: [] as { k: string, v: string }[], envFile: '',
  sandbox: 'none' as 'none' | 'standard' | 'strict', caches: [] as string[],
  health: '', restart: 'always', after: [] as string[],
})
const picker = ref(false)
const detect = ref<Detect | null>(null)
const preview = ref<Preview | null>(null)
const dns = ref<Record<string, { points_here: boolean | null, addresses: string[] }>>({})
const routers = ref<RouterInfo[]>([])
onMounted(async () => {
  routers.value = (await api<RoutersView>('/api/routers').catch(() => ({ routers: [] as RouterInfo[] }))).routers
  if (routers.value.length) f.router = routers.value[0].name
  // "Host a new app here" from the Router page: ?router=<name>&path=/reserved
  const q = useRoute().query
  if (typeof q.router === 'string' && routers.value.some(r => r.name === q.router)) {
    f.address = 'router'
    f.router = q.router
    await nextTick()
    if (typeof q.path === 'string' && pathItems.value.some(i => i.value === q.path)) f.routerPath = q.path
  }
})
const NEW_PATH = '+'
const router = computed(() => routers.value.find(r => r.name === f.router))
// reserved (unassigned) paths can be used as they are; "New path" makes one on the spot
const pathItems = computed(() => [
  ...(router.value?.entries.filter(e => !e.app).map(e => ({ label: `${e.path}/ (reserved)`, value: e.path })) ?? []),
  { label: 'New path…', value: NEW_PATH },
])
watch(router, () => { f.routerPath = pathItems.value[0]?.value ?? NEW_PATH })
const mountPath = computed(() => {
  if (f.address !== 'router' || !router.value) return ''
  const p = f.routerPath === NEW_PATH ? (f.newPath.trim() || `/${f.name}`) : f.routerPath
  return ('/' + p.toLowerCase().replace(/^\/+|\/+$/g, '')).replace(/\/{2,}/g, '/')
})
const creating = ref(false)
const progress = ref<string[]>([])

watch(() => f.workdir, async (p) => {
  if (!p) return
  if (!f.name) f.name = p.split('/').pop()!.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^[^a-z]+/, '').slice(0, 40)
  try {
    detect.value = await api<Detect>('/api/detect', { params: { path: p } })
    const d = detect.value
    if (d.venvs.length) { f.runtime = 'venv'; f.venv = d.venvs[0] }
    else if (d.uv) f.runtime = 'uv'
    else if (d.node) f.runtime = 'node'
    if (!f.command) f.command = d.node?.start ? 'npm start' : d.hints[0]?.command ?? ''
  } catch { detect.value = null }
})

const body = computed(() => {
  const b: Record<string, unknown> = { name: f.name, workdir: f.workdir }
  const domains = f.address === 'domain' ? f.domains.map(d => d.trim().toLowerCase()).filter(Boolean) : []
  const web = domains.length > 0 || !!mountPath.value
  if (mountPath.value) b.mount = { router: f.router, path: mountPath.value, strip: f.strip }
  if (domains.length) b.domains = domains
  const env = Object.fromEntries(f.env.filter(e => e.k.trim()).map(e => [e.k.trim(), e.v]))
  if (Object.keys(env).length) b.env = env
  if (f.envFile.trim()) b.env_file = [f.envFile.trim()]
  if (f.after.length) { b.after = [...f.after]; b.wants = [...f.after] }
  const routes: Record<string, unknown>[] = f.statics.filter(s => s.path && s.dir)
    .map(s => ({ path: s.path, static: s.dir, ...(s.spa ? { spa_fallback: '/index.html' } : {}) }))
  if (f.kind !== 'static') {
    const runtime: Record<string, unknown> = {}
    if (f.runtime === 'venv') runtime.venv = f.venv
    if (f.runtime === 'uv') runtime.uv = true
    if (f.runtime === 'node') runtime.node = f.node
    if (f.runtime === 'conda') runtime.conda = f.conda
    const proc: Record<string, unknown> = { command: f.command, restart: f.restart }
    if (Object.keys(runtime).length) proc.runtime = runtime
    if (web || f.health) proc.port = f.portMode === 'auto' ? 'auto' : Number(f.port)
    if (f.gpus.length) proc.gpus = [...f.gpus]
    if (f.health) proc.health = f.health
    b.processes = { main: proc }
    if (web) {
      const r: Record<string, unknown> = { path: '/', to: 'main', websocket: f.websocket, streaming: f.streaming }
      if (f.timeout) r.timeout = f.timeout
      routes.push(r)
    }
  }
  if (routes.length) b.routes = routes
  if (f.maxBody) b.proxy = { max_body: f.maxBody }
  if (f.sandbox !== 'none' || f.caches.length) b.sandbox = { level: f.sandbox, caches: [...f.caches] }
  return b
})

let t: ReturnType<typeof setTimeout> | undefined
watch(body, () => {
  clearTimeout(t)
  t = setTimeout(async () => {
    if (!f.name || !f.workdir || (f.kind !== 'static' && !f.command)) { preview.value = null; return }
    try { preview.value = await api<Preview>('/api/preview', { method: 'POST', body: body.value }) } catch (e) { preview.value = { ok: false, errors: [apiError(e)], toml: '' } }
  }, 500)
}, { deep: true })

async function checkDns(d: string) {
  d = d.trim().toLowerCase()
  if (!d.includes('.')) return
  try { dns.value[d] = await api('/api/dns', { params: { domain: d } }) } catch { /* shown as unknown */ }
}

async function create() {
  creating.value = true
  progress.value = ['Saving the manifest…']
  try {
    await api('/api/apps', { method: 'POST', body: body.value })
    progress.value.push('Setting up the service, web site and certificate…')
    const r = await api<{ log: string[] }>(`/api/apps/${f.name}/apply`, { method: 'POST' })
    progress.value.push(...r.log, 'Done.')
    toast.add({ title: `${f.name} is hosted`, color: 'success' })
    setTimeout(() => navigateTo('/rack'), 1200)
  } catch (e) {
    progress.value.push(...((e as { data?: { log?: string[] } }).data?.log ?? []), `Failed: ${apiError(e)}`)
    toast.add({ title: 'Hosting not finished', description: `${apiError(e)} The manifest was saved; fix it from the app's Config tab and apply again.`, color: 'error' })
  } finally {
    creating.value = false
  }
}

const runtimes = [
  { label: 'Plain command', value: 'plain' }, { label: 'Python venv', value: 'venv' }, { label: 'uv project', value: 'uv' },
  { label: 'Node (nvm)', value: 'node' }, { label: 'Conda env', value: 'conda' },
]
const kinds = [
  { label: 'App', value: 'app', description: 'A process that listens on a port' },
  { label: 'Static site', value: 'static', description: 'Files served by nginx, no process' },
  { label: 'App + static files', value: 'both', description: 'A process plus folders nginx serves directly' },
]
const cacheOpts = ['huggingface', 'torch', 'uv', 'pip', 'npm', 'playwright']
const sandboxes = [
  { label: 'None (like a hand-written service)', value: 'none' },
  { label: 'Standard (only its folder is writable)', value: 'standard' },
  { label: 'Strict (+ GPU and syscall limits)', value: 'strict' },
]
function toggle(list: string[], v: string) { const i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v) }
</script>

<template>
  <div class="max-w-[90rem] mx-auto">
    <header class="mb-6">
      <h1 class="silk text-3xl text-highlighted">New hosting</h1>
      <p class="text-sm text-muted mt-1">Describe the app once. AHost picks a port, writes the service and the web site, and gets the certificate.</p>
    </header>
    <div class="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,34rem)] items-start">
      <form class="space-y-5" @submit.prevent="create">
        <section class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-4">
          <h2 class="silk text-sm text-muted">Basics</h2>
          <UFormField label="Project folder" required>
            <div class="flex gap-2">
              <UInput v-model="f.workdir" placeholder="/home/…/my-project" class="grow" :ui="{ base: 'font-mono' }" />
              <UButton color="neutral" variant="soft" icon="i-lucide-folder-open" label="Browse" @click="picker = true" />
            </div>
          </UFormField>
          <UFormField label="Name" help="Lowercase letters, digits and dashes. Becomes ahost@<name>." required>
            <UInput v-model="f.name" class="w-full" :ui="{ base: 'font-mono' }" />
          </UFormField>
          <UFormField label="What is it?">
            <URadioGroup v-model="f.kind" :items="kinds" orientation="horizontal" />
          </UFormField>
        </section>

        <section v-if="f.kind !== 'static'" class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-4">
          <h2 class="silk text-sm text-muted">Run</h2>
          <div class="grid gap-4 sm:grid-cols-2">
            <UFormField label="Runtime" :help="detect ? `Detected: ${[detect.venvs.length && `venv (${detect.venvs.join(', ')})`, detect.uv && 'uv.lock', detect.node && 'package.json', detect.conda && 'environment.yml'].filter(Boolean).join(', ') || 'nothing special'}` : ''">
              <USelect v-model="f.runtime" :items="runtimes" class="w-full" />
            </UFormField>
            <UFormField v-if="f.runtime === 'venv'" label="venv folder"><UInput v-model="f.venv" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
            <UFormField v-if="f.runtime === 'node'" label="Node version"><UInput v-model="f.node" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
            <UFormField v-if="f.runtime === 'conda'" label="Conda env"><UInput v-model="f.conda" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
          </div>
          <UFormField label="Start command" help="Use $PORT where the app needs its port, e.g. uvicorn main:app --host 127.0.0.1 --port $PORT" required>
            <UInput v-model="f.command" class="w-full" :ui="{ base: 'font-mono' }" placeholder="python app.py --port $PORT" />
          </UFormField>
          <div class="grid gap-4 sm:grid-cols-3">
            <UFormField label="Port">
              <USelect v-model="f.portMode" :items="[{ label: 'Pick one for me', value: 'auto' }, { label: 'Use a specific port', value: 'fixed' }]" class="w-full" />
            </UFormField>
            <UFormField v-if="f.portMode === 'fixed'" label="Port number"><UInput v-model.number="f.port" type="number" class="w-full" /></UFormField>
            <UFormField label="Health check path" help="Optional, e.g. /healthz"><UInput v-model="f.health" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
          </div>
          <div class="flex flex-wrap gap-5">
            <UFormField label="Restart"><USelect v-model="f.restart" :items="[{ label: 'Always', value: 'always' }, { label: 'Only on failure', value: 'on-failure' }, { label: 'Never', value: 'no' }]" class="w-44" /></UFormField>
            <UFormField label="Start after">
              <div class="flex gap-3 pt-1.5">
                <UCheckbox label="PostgreSQL" :model-value="f.after.includes('postgresql.service')" @update:model-value="toggle(f.after, 'postgresql.service')" />
                <UCheckbox label="Redis" :model-value="f.after.includes('redis-server.service')" @update:model-value="toggle(f.after, 'redis-server.service')" />
              </div>
            </UFormField>
          </div>
        </section>

        <section class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-4">
          <h2 class="silk text-sm text-muted">Web</h2>
          <UFormField label="Where people reach it">
            <URadioGroup v-model="f.address" orientation="horizontal" :items="[
              { label: 'Its own domain', value: 'domain', description: 'app.example.com' },
              { label: 'A path on a router', value: 'router', description: routers.length ? `${routers[0].domain}/app` : 'Create a router first', disabled: !routers.length },
              { label: 'Not on the web', value: 'none', description: 'Workers, bots' },
            ]" />
          </UFormField>
          <div v-if="f.address === 'router' && router" class="grid gap-4 sm:grid-cols-2">
            <UFormField label="Router">
              <USelect v-model="f.router" :items="routers.map(r => ({ label: r.domain, value: r.name }))" class="w-full" :ui="{ base: 'font-mono' }" />
            </UFormField>
            <UFormField label="Path">
              <USelect v-model="f.routerPath" :items="pathItems" class="w-full" :ui="{ base: 'font-mono' }" />
            </UFormField>
            <UFormField v-if="f.routerPath === NEW_PATH" label="New path">
              <UInput v-model="f.newPath" :placeholder="`/${f.name || 'app'}`" class="w-full" :ui="{ base: 'font-mono' }" />
            </UFormField>
            <div class="sm:col-span-2 flex flex-wrap items-center gap-x-6 gap-y-2">
              <span class="jack">{{ router.domain }}{{ mountPath }}/</span>
              <USwitch v-model="f.strip" :label="`Strip ${mountPath}`" :description="f.strip ? 'The app sees / (redirects and cookies are fixed up)' : `The app is set up to live under ${mountPath}`" />
            </div>
          </div>
          <UFormField v-if="f.address === 'domain'" label="Domains" help="Point the DNS record at this server first; HTTPS is set up on apply.">
            <div class="space-y-2">
              <div v-for="(d, i) in f.domains" :key="i" class="flex gap-2 items-center">
                <UInput v-model="f.domains[i]" placeholder="app.example.com" class="grow" :ui="{ base: 'font-mono' }" @blur="checkDns(f.domains[i])" />
                <UBadge v-if="dns[d.trim().toLowerCase()]" :color="dns[d.trim().toLowerCase()].points_here ? 'success' : 'warning'" variant="soft" :label="dns[d.trim().toLowerCase()].points_here ? 'Points here' : (dns[d.trim().toLowerCase()].addresses.length ? 'Points elsewhere' : 'Not in DNS yet')" />
                <UButton v-if="f.domains.length > 1" size="xs" variant="ghost" color="neutral" icon="i-lucide-x" aria-label="Remove domain" @click="f.domains.splice(i, 1)" />
              </div>
              <UButton size="xs" variant="ghost" icon="i-lucide-plus" label="Add domain" @click="f.domains.push('')" />
            </div>
          </UFormField>
          <div v-if="f.kind !== 'static' && f.address !== 'none'" class="flex flex-wrap gap-6">
            <USwitch v-model="f.websocket" label="WebSockets" />
            <USwitch v-model="f.streaming" label="Streaming (SSE, LLM tokens)" description="No response buffering" />
          </div>
          <div class="grid gap-4 sm:grid-cols-2">
            <UFormField v-if="f.kind !== 'static'" label="Request timeout" help="Blank = 24h when streaming, else 60s"><UInput v-model="f.timeout" placeholder="e.g. 300s" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
            <UFormField label="Max upload size" help="Blank = 10m"><UInput v-model="f.maxBody" placeholder="e.g. 100m" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
          </div>
          <UFormField v-if="f.kind !== 'app'" label="Static folders" help="nginx serves these directly. Folder is relative to the project folder.">
            <div class="space-y-2">
              <div v-for="(s, i) in f.statics" :key="i" class="grid grid-cols-[8rem_1fr_auto_auto] gap-2 items-center">
                <UInput v-model="s.path" placeholder="/static/" :ui="{ base: 'font-mono' }" />
                <UInput v-model="s.dir" placeholder="dist" :ui="{ base: 'font-mono' }" />
                <UCheckbox v-model="s.spa" label="SPA" />
                <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-x" aria-label="Remove folder" @click="f.statics.splice(i, 1)" />
              </div>
              <UButton size="xs" variant="ghost" icon="i-lucide-plus" label="Add folder" @click="f.statics.push({ path: f.kind === 'static' && !f.statics.length ? '/' : '/static/', dir: '', spa: false })" />
            </div>
          </UFormField>
        </section>

        <section v-if="f.kind !== 'static' && overview?.gpus.length" class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-3">
          <h2 class="silk text-sm text-muted">GPUs</h2>
          <p class="text-sm text-muted">Pick the cards this app may use. None = it sees all of them.</p>
          <div class="grid gap-2 sm:grid-cols-2">
            <button
              v-for="g in overview.gpus" :key="g.uuid" type="button"
              class="text-left rounded-md border px-3 py-2 transition-colors"
              :class="f.gpus.includes(g.uuid) ? 'border-primary bg-[var(--ah-panel-2)]' : 'border-default hover:bg-[var(--ah-panel-2)]'"
              :aria-pressed="f.gpus.includes(g.uuid)"
              @click="toggle(f.gpus, g.uuid)"
            >
              <div class="flex justify-between"><span class="font-medium">{{ g.name }}</span><span class="data text-xs text-muted">#{{ g.index }}</span></div>
              <div class="h-1.5 mt-2 rounded bg-[var(--ah-rule)] overflow-hidden"><div class="h-full bg-primary" :style="{ width: `${100 * (g.used_mib ?? 0) / g.total_mib}%` }" /></div>
              <div class="data text-xs text-muted mt-1">{{ mib(g.used_mib) }} / {{ mib(g.total_mib) }}<template v-if="g.users.length"> · {{ g.users.map(u => u.name).join(', ') }}</template></div>
            </button>
          </div>
        </section>

        <section v-if="f.kind !== 'static'" class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-4">
          <h2 class="silk text-sm text-muted">Environment & isolation</h2>
          <UFormField label="Environment variables" help="Stored in the manifest. Keep real secrets in the app's own .env file.">
            <div class="space-y-2">
              <div v-for="(e, i) in f.env" :key="i" class="grid grid-cols-[12rem_1fr_auto] gap-2">
                <UInput v-model="e.k" placeholder="NAME" :ui="{ base: 'font-mono' }" />
                <UInput v-model="e.v" placeholder="value" :ui="{ base: 'font-mono' }" />
                <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-x" aria-label="Remove variable" @click="f.env.splice(i, 1)" />
              </div>
              <UButton size="xs" variant="ghost" icon="i-lucide-plus" label="Add variable" @click="f.env.push({ k: '', v: '' })" />
            </div>
          </UFormField>
          <UFormField label="Env file" help="Relative to the project folder, e.g. .env"><UInput v-model="f.envFile" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
          <UFormField label="Isolation"><USelect v-model="f.sandbox" :items="sandboxes" class="w-full" /></UFormField>
          <UFormField v-if="f.sandbox !== 'none'" label="Shared caches it may write" help="Anything else outside its folder is read-only or hidden, and does not survive a restart.">
            <div class="flex flex-wrap gap-3"><UCheckbox v-for="c in cacheOpts" :key="c" :label="c" :model-value="f.caches.includes(c)" @update:model-value="toggle(f.caches, c)" /></div>
          </UFormField>
        </section>
      </form>

      <aside class="xl:sticky xl:top-6 space-y-3">
        <div class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
          <h2 class="silk text-sm text-muted mb-3">What Apply will do</h2>
          <p v-if="!preview" class="text-sm text-dimmed">Pick a folder and a start command to see the plan.</p>
          <template v-else>
            <ul v-if="preview.errors.length" class="mb-3 space-y-1">
              <li v-for="e in preview.errors" :key="e" class="text-sm text-[var(--ah-fault)]">{{ e }}</li>
            </ul>
            <ul v-if="preview.warnings?.length" class="mb-3 space-y-1">
              <li v-for="w in preview.warnings" :key="w" class="text-sm text-[var(--ah-signal)]">{{ w }}</li>
            </ul>
            <ol v-if="preview.steps?.length" class="list-decimal pl-5 space-y-1 text-sm mb-3">
              <li v-for="s in preview.steps" :key="s">{{ s }}</li>
            </ol>
            <details class="text-sm">
              <summary class="cursor-pointer text-muted">Manifest and generated files</summary>
              <pre class="mt-2 rounded-md bg-[#10151a] text-[#cdd6de] p-3 text-[0.72rem] leading-5 overflow-auto max-h-[28rem]">{{ preview.toml }}{{ preview.text ? '\n\n' + preview.text : '' }}</pre>
            </details>
          </template>
        </div>
        <UButton block size="lg" icon="i-lucide-rocket" label="Create hosting" :disabled="!preview?.ok || creating" :loading="creating" @click="create" />
        <pre v-if="progress.length" class="rounded-md border border-default bg-[var(--ah-panel-2)] p-3 text-[0.75rem] leading-5 overflow-auto max-h-72">{{ progress.join('\n') }}</pre>
      </aside>
    </div>
    <FolderPicker v-model:open="picker" @pick="(p) => (f.workdir = p)" />
  </div>
</template>
