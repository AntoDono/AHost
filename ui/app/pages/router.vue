<script setup lang="ts">
// Routers: one shared domain, one path per app (apps.example.com/chat → chat). Paths can be reserved first
// and assigned later; every change is saved and made live in one step (nginx -t + reload, undone on failure).
const api = useApi()
const toast = useToast()
const { data: overview } = useOverview()
const view = ref<RoutersView | null>(null)
const loadError = ref('')
const busy = ref<Record<string, string | null>>({}) // router -> what's being saved
const NONE = '-'

async function load() {
  try {
    view.value = await api<RoutersView>('/api/routers')
    loadError.value = ''
  } catch (e) { loadError.value = apiError(e) }
}
onMounted(load)

const appByName = computed(() => new Map((overview.value?.apps ?? []).map(a => [a.name, a])))
const appItems = computed(() => [
  { label: 'Reserved', value: NONE },
  ...(view.value?.apps ?? []).map(n => ({ label: n, value: n })),
])
function port(name: string) { return appByName.value.get(name)?.processes.find(p => p.port)?.port ?? null }

// ---- saving: edit a copy, PUT the whole router, reload
type Draft = Pick<RouterInfo, 'domain' | 'description' | 'cert' | 'index' | 'max_body' | 'entries'>
async function save(r: RouterInfo, what: string, edit: (d: Draft) => void) {
  const d: Draft = JSON.parse(JSON.stringify({ domain: r.domain, description: r.description, cert: r.cert,
    index: r.index, max_body: r.max_body, entries: r.entries }))
  edit(d)
  busy.value[r.name] = what
  try {
    await api(`/api/routers/${r.name}`, { method: 'PUT', body: d })
    toast.add({ title: what, color: 'success' })
  } catch (e) {
    toast.add({ title: 'Not saved', description: apiError(e), color: 'error' })
  } finally {
    busy.value[r.name] = null
    await load()
  }
}
function assign(r: RouterInfo, e: RouterEntry, app: string) {
  const to = app === NONE ? null : app
  if (to === e.app) return
  save(r, to ? `${r.domain}${e.path}/ now goes to ${to}` : `${r.domain}${e.path}/ is reserved`, (d) => {
    d.entries.find(x => x.path === e.path)!.app = to
  })
}
function setStrip(r: RouterInfo, e: RouterEntry, strip: boolean) {
  save(r, strip ? `${e.path} is stripped before it reaches the app` : `${e.path} is passed through to the app`, (d) => {
    d.entries.find(x => x.path === e.path)!.strip = strip
  })
}
const confirmDrop = ref<string | null>(null) // `${router}:${path}` waiting for a second click
function drop(r: RouterInfo, e: RouterEntry) {
  const key = `${r.name}:${e.path}`
  if (e.app && confirmDrop.value !== key) { confirmDrop.value = key; return }
  confirmDrop.value = null
  save(r, `${e.path} removed`, (d) => {
    d.entries = d.entries.filter(x => x.path !== e.path)
    if (d.index === e.path) d.index = null
  })
}
function setIndex(r: RouterInfo, v: string) {
  save(r, v === NONE ? 'The bare domain now answers 404' : `The bare domain now opens ${v}/`, (d) => { d.index = v === NONE ? null : v })
}

// ---- add a path
const adding = reactive<Record<string, { path: string, app: string }>>({})
function addForm(r: RouterInfo) { return (adding[r.name] ??= { path: '', app: NONE }) }
function normPath(p: string) { return ('/' + p.trim().toLowerCase().replace(/^\/+|\/+$/g, '')).replace(/\/{2,}/g, '/') }
const PATH_OK = /^(\/[a-z0-9][a-z0-9._-]{0,62}){1,4}$/
function addPath(r: RouterInfo) {
  const a = addForm(r)
  const path = normPath(a.path)
  if (!PATH_OK.test(path)) { toast.add({ title: 'Use a path like /chat', description: 'Lowercase letters, digits, dots, dashes and underscores.', color: 'warning' }); return }
  if (r.entries.some(e => e.path === path)) { toast.add({ title: `${path} is already on this router`, color: 'warning' }); return }
  const app = a.app === NONE ? null : a.app
  save(r, app ? `${r.domain}${path}/ now goes to ${app}` : `${r.domain}${path}/ reserved`, (d) => {
    d.entries.push({ path, app, strip: true, note: '' })
  }).then(() => { a.path = ''; a.app = NONE })
}

// ---- create / delete routers
const creating = ref(false)
const showNew = ref(false)
const nr = reactive({ name: '', domain: '', description: '' })
const nrDns = ref<{ points_here: boolean | null, addresses: string[] } | null>(null)
watch(() => nr.domain, (d) => {
  nrDns.value = null
  if (!nr.name || nr.name === slug(nr.domain.split('.')[0] ?? '')) nr.name = slug(d.split('.')[0] ?? '')
})
function slug(s: string) { return s.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^[^a-z]+/, '').slice(0, 40) }
async function checkDns() {
  const d = nr.domain.trim().toLowerCase()
  if (d.includes('.')) nrDns.value = await api<{ points_here: boolean | null, addresses: string[] }>('/api/dns', { params: { domain: d } }).catch(() => null)
}
async function createRouter() {
  creating.value = true
  try {
    await api('/api/routers', { method: 'POST', body: { ...nr, domain: nr.domain.trim().toLowerCase() } })
    toast.add({ title: `https://${nr.domain} is ready`, description: 'Add paths and point them at apps.', color: 'success' })
    showNew.value = false
    Object.assign(nr, { name: '', domain: '', description: '' })
  } catch (e) {
    toast.add({ title: 'Router not created', description: apiError(e), color: 'error' })
  } finally {
    creating.value = false
    await load()
  }
}
async function applyRouter(r: RouterInfo) {
  busy.value[r.name] = 'Applying'
  try {
    await api(`/api/routers/${r.name}/apply`, { method: 'POST' })
    toast.add({ title: `${r.domain} is up to date`, color: 'success' })
  } catch (e) { toast.add({ title: 'Apply failed', description: apiError(e), color: 'error' }) } finally {
    busy.value[r.name] = null
    await load()
  }
}
const deleting = ref<RouterInfo | null>(null)
const deleteText = ref('')
async function deleteRouter() {
  const r = deleting.value!
  try {
    await api(`/api/routers/${r.name}`, { method: 'DELETE', params: { confirm: deleteText.value } })
    toast.add({ title: `${r.domain} taken down`, description: 'Its apps keep running.', color: 'success' })
    deleting.value = null
  } catch (e) { toast.add({ title: 'Not deleted', description: apiError(e), color: 'error' }) } finally {
    deleteText.value = ''
    await load()
  }
}
</script>

<template>
  <div class="max-w-6xl mx-auto space-y-6">
    <header class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="silk text-3xl text-highlighted">Router</h1>
        <p class="text-sm text-muted mt-1 max-w-2xl">One domain, many apps: each path leads to an app, like <span class="font-mono">apps.example.com/chat</span>. Reserve paths now and assign them whenever.</p>
      </div>
      <UButton v-if="!showNew" icon="i-lucide-plus" label="New router" @click="showNew = true" />
    </header>
    <UAlert v-if="loadError" color="error" variant="soft" icon="i-lucide-triangle-alert" :description="loadError" />

    <!-- new router -->
    <form v-if="showNew || (view && !view.routers.length)" class="rounded-lg border border-default bg-[var(--ah-panel)] p-5 space-y-4" @submit.prevent="createRouter">
      <div>
        <h2 class="font-medium text-highlighted">New router</h2>
        <p class="text-sm text-muted mt-0.5">Point the domain's DNS record at this server first. AHost sets up the site and its HTTPS certificate.</p>
      </div>
      <div class="grid gap-4 sm:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <UFormField label="Domain" required>
          <div class="flex gap-2 items-center">
            <UInput v-model="nr.domain" placeholder="apps.example.com" class="grow" :ui="{ base: 'font-mono' }" @blur="checkDns" />
            <UBadge v-if="nrDns" :color="nrDns.points_here ? 'success' : 'warning'" variant="soft" :label="nrDns.points_here ? 'Points here' : (nrDns.addresses.length ? 'Points elsewhere' : 'Not in DNS yet')" />
          </div>
        </UFormField>
        <UFormField label="Name" help="Used for its file and certificate."><UInput v-model="nr.name" class="w-full" :ui="{ base: 'font-mono' }" /></UFormField>
      </div>
      <UFormField label="Description"><UInput v-model="nr.description" placeholder="Optional" class="w-full" /></UFormField>
      <div class="flex gap-2 justify-end">
        <UButton v-if="view?.routers.length" color="neutral" variant="ghost" label="Cancel" @click="showNew = false" />
        <UButton type="submit" icon="i-lucide-split" :label="creating ? 'Creating (getting the certificate)…' : 'Create router'" :loading="creating" :disabled="!nr.domain.includes('.') || !nr.name" />
      </div>
    </form>

    <UAlert v-for="(err, name) in view?.invalid" :key="name" color="error" variant="soft" icon="i-lucide-file-warning" :title="`routers/${name}.toml can't be read`" :description="err" />

    <!-- one card per router: the domain is the trunk, each path a branch -->
    <section v-for="r in view?.routers" :key="r.name" class="rounded-lg border border-default bg-[var(--ah-panel)] p-4 md:p-5" :aria-label="`Router ${r.domain}`">
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span class="silk text-xs text-muted">Router · {{ r.name }}</span>
        <span v-if="busy[r.name]" class="flex items-center gap-1.5 text-xs text-muted"><span class="led pulsing" data-state="starting" />{{ busy[r.name] }}…</span>
        <div class="ml-auto flex items-center gap-1">
          <UTooltip text="Open"><UButton :to="r.url" target="_blank" size="sm" variant="ghost" color="neutral" icon="i-lucide-external-link" :aria-label="`Open ${r.domain}`" /></UTooltip>
          <UTooltip text="Delete router"><UButton size="sm" variant="ghost" color="neutral" icon="i-lucide-trash-2" aria-label="Delete router" @click="deleting = r; deleteText = ''" /></UTooltip>
        </div>
      </div>

      <div class="trunk mt-2 flex flex-wrap items-center gap-2">
        <span class="jack trunk-jack text-base text-highlighted">{{ r.domain }}</span>
        <UBadge v-if="!r.pending" color="success" variant="soft" size="sm" icon="i-lucide-lock" label="Live" />
        <UBadge v-else color="warning" variant="soft" size="sm" label="Not applied" />
        <span v-if="r.description" class="text-sm text-muted">{{ r.description }}</span>
      </div>

      <ol class="bus mt-1" :aria-label="`Paths on ${r.domain}`">
        <li
          v-for="e in r.entries" :key="e.path"
          class="branch group"
          :class="{ reserved: !e.app, missing: e.app && !appByName.get(e.app) }"
          :style="{ '--cable': e.app ? cableColor(e.app) : 'var(--ah-dim)' }"
        >
          <a class="jack path hover:!border-primary" :href="`${r.url}${e.path}/`" target="_blank" rel="noopener" :title="`Open ${r.domain}${e.path}/`">{{ e.path }}/</a>
          <span class="cable" aria-hidden="true" />
          <USelect
            :model-value="e.app ?? NONE" :items="appItems" size="sm" class="w-48"
            :ui="{ base: 'font-mono' }" :disabled="!!busy[r.name]" :aria-label="`App for ${e.path}`"
            @update:model-value="(v: string) => assign(r, e, v)"
          />
          <span class="flex items-center gap-2 text-xs text-muted min-w-0">
            <template v-if="e.app && appByName.get(e.app)">
              <span class="led" :data-state="appByName.get(e.app)!.state" />
              <span>{{ STATE_LABEL[appByName.get(e.app)!.state] }}</span>
              <span v-if="port(e.app)" class="data text-xs">:{{ port(e.app) }}</span>
            </template>
            <span v-else-if="e.app" class="text-[var(--ah-fault)]">no such app: answers 404</span>
            <span v-else>answers 404 until you pick an app or <NuxtLink :to="{ path: '/new', query: { router: r.name, path: e.path } }" class="text-primary hover:underline">host a new one here</NuxtLink></span>
          </span>
          <span class="ml-auto flex items-center gap-2">
            <UTooltip v-if="e.app" :text="e.strip ? `The app sees / instead of ${e.path}/. Its redirects and cookies are moved under ${e.path}/.` : `The app sees ${e.path}/ as is. Use this when the app is set up to live under ${e.path}.`">
              <USwitch :model-value="e.strip" size="sm" label="Strip path" :disabled="!!busy[r.name]" @update:model-value="(v: boolean) => setStrip(r, e, v)" />
            </UTooltip>
            <UButton
              size="xs" :variant="confirmDrop === `${r.name}:${e.path}` ? 'soft' : 'ghost'" :color="confirmDrop === `${r.name}:${e.path}` ? 'error' : 'neutral'"
              :icon="confirmDrop === `${r.name}:${e.path}` ? undefined : 'i-lucide-x'"
              :label="confirmDrop === `${r.name}:${e.path}` ? `Remove ${e.path}?` : undefined"
              :aria-label="`Remove ${e.path}`" :disabled="!!busy[r.name]" @click="drop(r, e)" @blur="confirmDrop = null"
            />
          </span>
        </li>

        <!-- add a path -->
        <li class="branch reserved adding">
          <form class="contents" @submit.prevent="addPath(r)">
            <UInput v-model="addForm(r).path" placeholder="/path" size="sm" class="w-40" :ui="{ base: 'font-mono' }" :aria-label="`New path on ${r.domain}`" />
            <span class="cable" aria-hidden="true" />
            <USelect v-model="addForm(r).app" :items="appItems" size="sm" class="w-48" :ui="{ base: 'font-mono' }" aria-label="App for the new path" />
            <UButton type="submit" size="sm" variant="soft" icon="i-lucide-plus" :label="addForm(r).app === NONE ? 'Reserve path' : 'Add path'" :disabled="!addForm(r).path.trim() || !!busy[r.name]" />
          </form>
        </li>
      </ol>

      <div class="mt-4 pt-4 border-t border-default flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
        <label class="flex items-center gap-2 text-muted">
          <span><span class="data text-xs">{{ r.domain }}/</span> opens</span>
          <USelect :model-value="r.index ?? NONE" size="sm" class="w-44" :ui="{ base: 'font-mono' }" :disabled="!!busy[r.name]"
            :items="[{ label: 'nothing (404)', value: NONE }, ...r.entries.map(e => ({ label: `${e.path}/`, value: e.path }))]"
            @update:model-value="(v: string) => setIndex(r, v)" />
        </label>
        <UButton v-if="r.pending" size="sm" color="warning" variant="soft" icon="i-lucide-refresh-cw" label="Apply now" :loading="busy[r.name] === 'Applying'" @click="applyRouter(r)" />
        <details class="ml-auto text-muted">
          <summary class="cursor-pointer text-xs">nginx config</summary>
          <pre class="mt-2 rounded-md bg-[#10151a] text-[#cdd6de] p-3 text-[0.72rem] leading-5 overflow-auto max-h-[28rem] max-w-[calc(100vw-4rem)]">{{ r.config }}</pre>
        </details>
      </div>
      <ul v-if="r.errors.length || r.warnings.length" class="mt-3 space-y-1 text-sm">
        <li v-for="x in r.errors" :key="x" class="text-[var(--ah-fault)]">{{ x }}</li>
        <li v-for="x in r.warnings" :key="x" class="text-[var(--ah-signal)]">{{ x }}</li>
      </ul>
    </section>

    <p v-if="view?.routers.length" class="text-xs text-dimmed max-w-3xl">
      Apps under one router share a domain, so they share cookies and browser storage. Keep apps that shouldn't trust each other on their own domains.
      Apps that build absolute links (like <span class="font-mono">/static/app.js</span>) need their base-path setting; AHost sends the prefix as <span class="font-mono">X-Forwarded-Prefix</span>.
    </p>

    <UModal :open="!!deleting" title="Delete router?" :description="`${deleting?.domain} stops answering. The apps keep running on their own domains. Type ${deleting?.name} to confirm.`" @update:open="(v: boolean) => { if (!v) deleting = null }">
      <template #body>
        <UInput v-model="deleteText" :placeholder="deleting?.name" class="w-full" autofocus />
      </template>
      <template #footer>
        <div class="flex justify-end gap-2 w-full">
          <UButton color="neutral" variant="ghost" label="Cancel" @click="deleting = null" />
          <UButton color="error" label="Delete router" :disabled="deleteText !== deleting?.name" @click="deleteRouter" />
        </div>
      </template>
    </UModal>
  </div>
</template>

<style scoped>
/* the domain is a trunk line running down the left; every path branches off it */
.trunk-jack { padding: 0.25rem 0.75rem; border-color: color-mix(in srgb, var(--ah-cable) 55%, var(--ah-rule)); }
.bus { position: relative; margin-left: 1.1rem; padding-top: 0.5rem; }
.bus::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 1.35rem; width: 2px; border-radius: 2px;
  background: color-mix(in srgb, var(--ah-cable) 45%, var(--ah-rule));
}
.branch {
  position: relative; display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem 0.75rem;
  padding: 0.4rem 0.5rem 0.4rem 1.6rem; border-radius: 0.5rem;
  transition: background-color 120ms ease;
}
.branch::before {
  content: ""; position: absolute; left: 0; top: 1.35rem; width: 1.2rem; height: 2px; border-radius: 2px;
  background: color-mix(in srgb, var(--cable) 55%, var(--ah-rule));
}
.branch:hover { background: var(--ah-panel-2); }
.branch .cable { flex: 0 0 2.25rem; min-width: 2.25rem; }
.branch:hover .cable, .branch:focus-within .cable { background: var(--cable); box-shadow: 0 0 6px color-mix(in srgb, var(--cable) 55%, transparent); }
.branch:hover .cable::after, .branch:focus-within .cable::after { border-left-color: var(--cable); }
.jack.path { min-width: 7rem; color: var(--ah-ink); }
.branch:not(.reserved) .jack.path { border-color: color-mix(in srgb, var(--cable) 55%, var(--ah-rule)); }

/* reserved: an unplugged, dashed lead */
.reserved .cable, .reserved::before {
  background: repeating-linear-gradient(90deg, var(--ah-rule) 0 4px, transparent 4px 8px) !important;
  box-shadow: none !important;
}
.reserved .cable::after { display: none; }
.missing .jack.path { border-color: var(--ah-fault); }
.adding:hover { background: transparent; }
@media (prefers-reduced-motion: reduce) { .branch { transition: none; } }
</style>
