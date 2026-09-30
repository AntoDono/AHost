<script setup lang="ts">
const open = defineModel<boolean>('open', { default: false })
const emit = defineEmits<{ pick: [path: string] }>()
const api = useApi()
const cwd = ref<{ path: string, parent: string | null, dirs: string[], roots: string[] } | null>(null)
const error = ref('')
async function go(path = '') {
  error.value = ''
  try { cwd.value = await api('/api/fs', { params: path ? { path } : {} }) } catch (e) { error.value = apiError(e) }
}
watch(open, (v) => { if (v && !cwd.value) go() })
const atRoot = computed(() => !cwd.value || cwd.value.roots.includes(cwd.value.path))
</script>

<template>
  <UModal v-model:open="open" title="Choose the project folder">
    <template #body>
      <div v-if="cwd" class="space-y-2">
        <div class="flex items-center gap-2">
          <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-arrow-up" aria-label="Up one folder" :disabled="atRoot" @click="go(cwd.parent!)" />
          <span class="data text-xs truncate">{{ cwd.path }}</span>
        </div>
        <p v-if="error" class="text-sm text-[var(--ah-fault)]">{{ error }}</p>
        <ul class="max-h-80 overflow-auto rounded-md border border-default divide-y divide-[var(--ah-rule)]">
          <li v-for="d in cwd.dirs" :key="d">
            <button class="w-full flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-[var(--ah-panel-2)] text-left" @click="go(`${cwd.path}/${d}`)">
              <UIcon name="i-lucide-folder" class="size-4 text-muted" />{{ d }}
            </button>
          </li>
          <li v-if="!cwd.dirs.length" class="px-3 py-2 text-sm text-dimmed">No subfolders</li>
        </ul>
      </div>
    </template>
    <template #footer>
      <div class="flex justify-end gap-2 w-full">
        <UButton color="neutral" variant="ghost" label="Cancel" @click="open = false" />
        <UButton label="Use this folder" :disabled="!cwd" @click="emit('pick', cwd!.path); open = false" />
      </div>
    </template>
  </UModal>
</template>
