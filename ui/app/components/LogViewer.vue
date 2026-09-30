<script setup lang="ts">
interface Line { ts?: number, unit?: string, pri?: number, msg: string }
const props = defineProps<{ app: AppInfo }>()
const api = useApi()

const source = ref<'app' | 'access' | 'error'>('app')
const process = ref<string>('all')
const range = ref<'restart' | '1h' | '24h' | 'boot'>('restart')
const level = ref<'all' | 'warn' | 'error'>('all')
const query = ref('')
const follow = ref(true)
const lines = ref<Line[]>([])
const loading = ref(false)
const error = ref('')
const box = ref<HTMLElement | null>(null)
let es: EventSource | null = null

const sources = [
  { label: 'App output', value: 'app' },
  { label: 'Web requests', value: 'access' },
  { label: 'Web errors', value: 'error' },
]
const ranges = [
  { label: 'Since last restart', value: 'restart' },
  { label: 'Last hour', value: '1h' },
  { label: 'Last 24 hours', value: '24h' },
  { label: 'Since boot', value: 'boot' },
]
const levels = [
  { label: 'All levels', value: 'all' },
  { label: 'Warnings + errors', value: 'warn' },
  { label: 'Errors only', value: 'error' },
]
const processes = computed(() => [{ label: 'All processes', value: 'all' },
  ...props.app.processes.map(p => ({ label: p.id, value: p.id }))])

function isError(l: Line) {
  if (l.pri != null) return l.pri <= 3
  return /\b(error|crit|emerg|alert)\b|" 5\d\d /i.test(l.msg)
}
function isWarn(l: Line) {
  if (l.pri != null) return l.pri === 4
  return /\bwarn|" 4\d\d /i.test(l.msg)
}
const shown = computed(() => {
  const q = query.value.toLowerCase()
  return lines.value.filter(l =>
    (level.value === 'all' || isError(l) || (level.value === 'warn' && isWarn(l)))
    && (!q || l.msg.toLowerCase().includes(q)))
})

function fmt(ts?: number) {
  if (!ts) return ''
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour12: false }) + (Date.now() - ts > 86400000 ? ` ${d.toLocaleDateString()}` : '')
}

function nearBottom() {
  const b = box.value
  return !b || b.scrollHeight - b.scrollTop - b.clientHeight < 60
}
function toBottom() {
  nextTick(() => { if (box.value) box.value.scrollTop = box.value.scrollHeight })
}

function stopStream() { es?.close(); es = null }
function startStream() {
  stopStream()
  if (source.value !== 'app' || !follow.value) return
  const q = process.value !== 'all' ? `?process=${encodeURIComponent(process.value)}` : ''
  es = new EventSource(`/api/apps/${props.app.name}/logs/stream${q}`)
  es.onmessage = (ev) => {
    const stick = nearBottom()
    lines.value.push(JSON.parse(ev.data))
    if (lines.value.length > 5000) lines.value.splice(0, lines.value.length - 5000)
    if (stick) toBottom()
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params: Record<string, string> = { source: source.value, lines: '800' }
    if (process.value !== 'all') params.process = process.value
    if (source.value === 'app') params.since = range.value
    lines.value = (await api<{ lines: Line[] }>(`/api/apps/${props.app.name}/logs`, { params })).lines
    toBottom()
  } catch (e) {
    error.value = apiError(e)
  } finally {
    loading.value = false
  }
  startStream()
}

let poll: ReturnType<typeof setInterval> | null = null
watch([source, follow], () => {
  if (poll) clearInterval(poll)
  poll = source.value !== 'app' && follow.value ? setInterval(load, 4000) : null
})
watch([source, process, range], load)
watch(follow, startStream)
onMounted(load)
onBeforeUnmount(() => { stopStream(); if (poll) clearInterval(poll) })

function text() {
  return shown.value.map(l => `${l.ts ? new Date(l.ts).toISOString() + ' ' : ''}${l.msg}`).join('\n')
}
async function copy() {
  await navigator.clipboard.writeText(text())
  useToast().add({ title: `Copied ${shown.value.length} lines`, color: 'neutral' })
}
function download() {
  const a = document.createElement('a')
  a.href = URL.createObjectURL(new Blob([text()], { type: 'text/plain' }))
  a.download = `${props.app.name}-${source.value}-${new Date().toISOString().slice(0, 19)}.log`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="flex flex-col gap-3 h-full min-h-0">
    <div class="flex flex-wrap items-center gap-2">
      <USelect v-model="source" :items="sources" size="sm" class="w-40" />
      <USelect v-if="app.multi && source === 'app'" v-model="process" :items="processes" size="sm" class="w-36" />
      <USelect v-if="source === 'app'" v-model="range" :items="ranges" size="sm" class="w-44" />
      <USelect v-model="level" :items="levels" size="sm" class="w-44" />
      <UInput v-model="query" size="sm" icon="i-lucide-search" placeholder="Search" class="w-40 grow" />
    </div>
    <div class="flex items-center justify-between gap-2 text-xs text-muted">
      <USwitch v-model="follow" size="sm" :label="source === 'app' ? 'Follow live' : 'Refresh every 4s'" />
      <span class="data text-xs">{{ shown.length }} / {{ lines.length }} lines</span>
      <div class="flex gap-1">
        <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-copy" label="Copy" @click="copy" />
        <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-download" label="Download" @click="download" />
      </div>
    </div>
    <p v-if="error" class="text-sm text-[var(--ah-fault)]">{{ error }}</p>
    <div ref="box" class="flex-1 min-h-[18rem] overflow-auto rounded-md border border-default bg-[#10151a] p-3 font-mono text-[0.78rem] leading-5 text-[#cdd6de]" role="log" aria-live="off">
      <div v-if="loading && !lines.length" class="text-[#7d8894]">Loading…</div>
      <div v-else-if="!shown.length" class="text-[#7d8894]">
        {{ lines.length ? 'No lines match the filters.' : 'Nothing logged in this range.' }}
      </div>
      <div
        v-for="(l, i) in shown" :key="i"
        class="whitespace-pre-wrap break-all"
        :class="isError(l) ? 'text-[#ff8a80]' : isWarn(l) ? 'text-[#ffd479]' : ''"
      ><span v-if="l.ts" class="text-[#6f7b87] select-none mr-3">{{ fmt(l.ts) }}</span><span v-if="app.multi && l.unit && process === 'all'" class="text-[#86aff1] select-none mr-2">{{ l.unit.replace(`ahost@${app.name}:`, '').replace('.service', '') }}</span>{{ l.msg }}</div>
    </div>
  </div>
</template>
