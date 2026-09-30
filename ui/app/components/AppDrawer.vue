<script setup lang="ts">
const props = defineProps<{ app: AppInfo | null, busy?: string | null }>()
const open = defineModel<boolean>('open', { default: false })
const tab = defineModel<string>('tab', { default: 'overview' })
const emit = defineEmits<{ action: [name: string, action: 'start' | 'stop' | 'restart'], changed: [] }>()
const api = useApi()
const toast = useToast()

const tabs = [
  { label: 'Overview', value: 'overview', icon: 'i-lucide-panel-top' },
  { label: 'Logs', value: 'logs', icon: 'i-lucide-scroll-text' },
  { label: 'Config', value: 'config', icon: 'i-lucide-file-code' },
]

// ---- config
const manifest = ref('')
const original = ref('')
const planText = ref('')
const applyLog = ref<string[]>([])
const saving = ref(false)
const applying = ref(false)
async function loadManifest() {
  if (!props.app) return
  const r = await api<{ toml: string }>(`/api/apps/${props.app.name}/manifest`)
  manifest.value = original.value = r.toml
  planText.value = ''
}
watch([tab, () => props.app?.name], () => { if (tab.value === 'config' && open.value) loadManifest() }, { immediate: true })

async function save() {
  if (!props.app) return
  saving.value = true
  try {
    await api(`/api/apps/${props.app.name}/manifest`, { method: 'PUT', body: { toml: manifest.value } })
    original.value = manifest.value
    planText.value = (await api<{ text: string }>(`/api/apps/${props.app.name}/plan`)).text
    toast.add({ title: 'Changes saved', description: 'Review what Apply will do, then apply.', color: 'success' })
  } catch (e) {
    toast.add({ title: 'Not saved', description: apiError(e), color: 'error' })
  } finally {
    saving.value = false
  }
}
async function preview() {
  if (!props.app) return
  planText.value = (await api<{ text: string }>(`/api/apps/${props.app.name}/plan`)).text
}
async function apply() {
  if (!props.app) return
  applying.value = true
  applyLog.value = []
  try {
    const r = await api<{ log: string[] }>(`/api/apps/${props.app.name}/apply`, { method: 'POST' })
    applyLog.value = r.log
    toast.add({ title: 'Changes applied', color: 'success' })
    emit('changed')
  } catch (e) {
    applyLog.value = [...((e as { data?: { log?: string[] } }).data?.log ?? []), apiError(e)]
    toast.add({ title: 'Apply failed', description: apiError(e), color: 'error' })
  } finally {
    applying.value = false
  }
}

// ---- remove from hosting
const confirmRemove = ref(false)
const confirmText = ref('')
const removing = ref(false)
async function remove() {
  if (!props.app) return
  removing.value = true
  try {
    await api(`/api/apps/${props.app.name}`, { method: 'DELETE', params: { confirm: confirmText.value } })
    toast.add({ title: `${props.app.name} removed from hosting`, description: 'Project folder, data and certificate were kept.', color: 'success' })
    confirmRemove.value = false
    open.value = false
    emit('changed')
  } catch (e) {
    toast.add({ title: 'Not removed', description: apiError(e), color: 'error' })
  } finally {
    removing.value = false
  }
}
const running = computed(() => props.app && ['running', 'unhealthy', 'starting'].includes(props.app.state))
</script>

<template>
  <USlideover v-model:open="open" side="right" :title="app?.name" :description="app?.description || app?.workdir" :ui="{ content: 'max-w-4xl w-full' }">
    <template #body>
      <div v-if="app" class="flex flex-col gap-4 h-full min-h-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="led" :data-state="app.state" />
          <span class="text-sm">{{ STATE_LABEL[app.state] }}</span>
          <div class="ml-auto flex gap-1.5">
            <UButton v-if="!running" size="sm" icon="i-lucide-play" label="Start" :loading="busy === 'start'" @click="emit('action', app.name, 'start')" />
            <UButton v-if="running" size="sm" color="neutral" variant="soft" icon="i-lucide-rotate-cw" label="Restart" :loading="busy === 'restart'" @click="emit('action', app.name, 'restart')" />
            <UButton v-if="running" size="sm" color="neutral" variant="soft" icon="i-lucide-square" label="Stop" :loading="busy === 'stop'" @click="emit('action', app.name, 'stop')" />
          </div>
        </div>
        <UTabs v-model="tab" :items="tabs" :content="false" size="sm" variant="link" />

        <!-- overview -->
        <div v-if="tab === 'overview'" class="space-y-5 overflow-auto">
          <dl class="grid grid-cols-[8rem_1fr] gap-y-2 text-sm">
            <dt class="text-muted">Addresses</dt>
            <dd class="flex flex-wrap gap-1.5">
              <a v-for="d in app.domains" :key="d" :href="`https://${d}`" target="_blank" rel="noopener" class="jack hover:!border-primary">{{ d }} ↗</a>
              <a v-for="m in app.mounts" :key="m" :href="`https://${m}/`" target="_blank" rel="noopener" class="jack hover:!border-primary">{{ m }}/ ↗</a>
              <span v-if="!app.domains.length && !app.mounts.length" class="text-dimmed">none (not on the web)</span>
            </dd>
            <dt class="text-muted">Folder</dt><dd class="data break-all">{{ app.workdir }}</dd>
            <dt class="text-muted">Runs as</dt><dd>{{ app.user }}<span v-if="app.user === 'root'" class="text-[var(--ah-signal)]"> (root)</span></dd>
            <dt class="text-muted">Isolation</dt><dd>{{ app.sandbox === 'none' ? 'None' : app.sandbox }}</dd>
            <template v-if="app.legacy">
              <dt class="text-muted">Adopted from</dt><dd class="data">{{ app.legacy.unit }}<span v-if="app.legacy.site"> + nginx {{ app.legacy.site }}</span></dd>
            </template>
          </dl>
          <div>
            <h3 class="silk text-sm text-muted mb-2">Processes</h3>
            <div class="overflow-x-auto rounded-md border border-default">
              <table class="w-full text-sm">
                <thead class="bg-[var(--ah-panel-2)] text-muted text-xs">
                  <tr><th class="text-left px-3 py-2 font-medium">Process</th><th class="text-left px-3 py-2 font-medium">State</th><th class="text-left px-3 py-2 font-medium">Port</th><th class="text-left px-3 py-2 font-medium">Memory</th><th class="text-left px-3 py-2 font-medium">Up</th><th class="text-left px-3 py-2 font-medium">Restarts</th><th class="text-left px-3 py-2 font-medium">Health</th></tr>
                </thead>
                <tbody>
                  <tr v-for="p in app.processes" :key="p.id" class="border-t border-default">
                    <td class="px-3 py-2"><span class="data">{{ p.unit }}</span><div class="text-xs text-muted data truncate max-w-[22rem]" :title="p.command">{{ p.command }}</div></td>
                    <td class="px-3 py-2">{{ p.active }}<span class="text-muted"> ({{ p.sub }})</span></td>
                    <td class="px-3 py-2 data">{{ p.port ?? '–' }}</td>
                    <td class="px-3 py-2 data">{{ bytes(p.memory) }}</td>
                    <td class="px-3 py-2 data">{{ p.active === 'active' ? since(p.since) : '–' }}</td>
                    <td class="px-3 py-2 data">{{ p.restarts }}</td>
                    <td class="px-3 py-2 data">{{ p.health ? (p.health.ok ? `${p.health.status} · ${p.health.ms} ms` : (p.health.detail || p.health.status)) : '–' }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div class="rounded-md border border-[var(--ah-fault)]/40 p-4">
            <h3 class="font-medium mb-1">Remove from hosting</h3>
            <p class="text-sm text-muted mb-3">Stops the service and removes its generated config, web site and port. The project folder, its data and the certificate are kept.</p>
            <UButton color="error" variant="soft" icon="i-lucide-trash-2" label="Remove from hosting…" @click="confirmRemove = true; confirmText = ''" />
          </div>
        </div>

        <!-- logs -->
        <LogViewer v-else-if="tab === 'logs'" :key="app.name" :app="app" class="flex-1 min-h-0" />

        <!-- config -->
        <div v-else class="flex flex-col gap-3 min-h-0 flex-1">
          <p class="text-sm text-muted">This is the app's manifest. Saving checks it first; nothing changes on the server until you apply.</p>
          <UTextarea v-model="manifest" :rows="18" autoresize :maxrows="30" class="w-full" :ui="{ base: 'font-mono text-[0.8rem] leading-5' }" spellcheck="false" />
          <div class="flex flex-wrap gap-2">
            <UButton icon="i-lucide-save" label="Save changes" :disabled="manifest === original" :loading="saving" @click="save" />
            <UButton color="neutral" variant="soft" icon="i-lucide-list-checks" label="What would change" @click="preview" />
            <UButton color="neutral" variant="soft" icon="i-lucide-rocket" label="Apply changes" :disabled="manifest !== original" :loading="applying" @click="apply" />
          </div>
          <pre v-if="planText" class="rounded-md border border-default bg-[#10151a] text-[#cdd6de] p-3 text-[0.75rem] leading-5 overflow-auto max-h-80">{{ planText }}</pre>
          <pre v-if="applyLog.length" class="rounded-md border border-default bg-[var(--ah-panel-2)] p-3 text-[0.75rem] leading-5 overflow-auto max-h-60">{{ applyLog.join('\n') }}</pre>
        </div>
      </div>
    </template>
  </USlideover>

  <UModal v-model:open="confirmRemove" title="Remove from hosting?" :description="`Type ${app?.name} to confirm.`">
    <template #body>
      <UInput v-model="confirmText" :placeholder="app?.name" class="w-full" autofocus />
    </template>
    <template #footer>
      <div class="flex justify-end gap-2 w-full">
        <UButton color="neutral" variant="ghost" label="Cancel" @click="confirmRemove = false" />
        <UButton color="error" label="Remove from hosting" :disabled="confirmText !== app?.name" :loading="removing" @click="remove" />
      </div>
    </template>
  </UModal>
</template>
