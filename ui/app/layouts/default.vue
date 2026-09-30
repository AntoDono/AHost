<script setup lang="ts">
const route = useRoute()
const user = useState<string | null | undefined>('user')
const api = useApi()
const { data } = useOverview()
const colorMode = useColorMode()

const nav = [
  { to: '/rack', label: 'Rack', icon: 'i-lucide-server' },
  { to: '/system', label: 'System', icon: 'i-lucide-activity' },
  { to: '/ports', label: 'Ports', icon: 'i-lucide-cable' },
  { to: '/new', label: 'New hosting', icon: 'i-lucide-plus' },
]
const counts = computed(() => {
  const apps = data.value?.apps ?? []
  return { running: apps.filter(a => a.state === 'running').length, total: apps.length,
    trouble: apps.filter(a => ['failed', 'unhealthy'].includes(a.state)).length }
})
const mem = computed(() => {
  const s = data.value?.system
  return s ? Math.round(100 * (1 - s.mem_available / s.mem_total)) : null
})

async function signOut() {
  await api('/api/auth/logout', { method: 'POST' }).catch(() => {})
  user.value = null
  await navigateTo('/login')
}
</script>

<template>
  <div class="min-h-dvh md:grid md:grid-cols-[13.5rem_1fr]">
    <div class="md:border-r border-default bg-[var(--ah-panel)]">
    <aside class="md:sticky md:top-0 md:h-dvh flex md:flex-col gap-1 border-b md:border-b-0 border-default px-3 py-3 md:py-5 overflow-x-auto">
      <NuxtLink to="/rack" class="flex items-center gap-2 px-2 md:mb-6 shrink-0" aria-label="AHost rack">
        <span class="grid grid-cols-2 gap-[3px]" aria-hidden="true">
          <span class="screw" /><span class="screw" /><span class="screw" /><span class="screw" />
        </span>
        <span class="silk text-lg tracking-[0.14em]">AHost</span>
      </NuxtLink>
      <nav class="flex md:flex-col gap-1">
        <NuxtLink
          v-for="n in nav" :key="n.to" :to="n.to"
          class="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-sm whitespace-nowrap text-muted hover:text-highlighted hover:bg-[var(--ah-panel-2)] focus-visible:outline-2 focus-visible:outline-primary"
          :class="route.path === n.to ? 'bg-[var(--ah-panel-2)] text-highlighted font-medium' : ''"
        >
          <UIcon :name="n.icon" class="size-4" />{{ n.label }}
        </NuxtLink>
      </nav>

      <div class="hidden md:block mt-auto space-y-3 px-2 text-xs text-muted">
        <div v-if="data" class="space-y-1.5">
          <div class="silk text-[0.7rem] text-dimmed">{{ data.system.hostname }}</div>
          <div class="flex justify-between"><span>Apps up</span><span class="data text-xs">{{ counts.running }}/{{ counts.total }}</span></div>
          <div v-if="counts.trouble" class="flex justify-between text-[var(--ah-fault)]"><span>Need attention</span><span class="data text-xs">{{ counts.trouble }}</span></div>
          <div class="flex justify-between"><span>Load</span><span class="data text-xs">{{ data.system.load[0] }}</span></div>
          <div class="flex justify-between"><span>Memory</span><span class="data text-xs">{{ mem }}%</span></div>
        </div>
        <div class="flex items-center justify-between border-t border-default pt-3">
          <span class="truncate">{{ user }}</span>
          <div class="flex gap-1">
            <UTooltip :text="colorMode.value === 'dark' ? 'Light mode' : 'Dark mode'">
              <UButton size="xs" variant="ghost" color="neutral" :icon="colorMode.value === 'dark' ? 'i-lucide-sun' : 'i-lucide-moon'" :aria-label="colorMode.value === 'dark' ? 'Light mode' : 'Dark mode'" @click="colorMode.preference = colorMode.value === 'dark' ? 'light' : 'dark'" />
            </UTooltip>
            <UTooltip text="Sign out">
              <UButton size="xs" variant="ghost" color="neutral" icon="i-lucide-log-out" aria-label="Sign out" @click="signOut" />
            </UTooltip>
          </div>
        </div>
      </div>
      <UButton class="md:hidden ml-auto shrink-0" size="xs" variant="ghost" color="neutral" icon="i-lucide-log-out" aria-label="Sign out" @click="signOut" />
    </aside>
    </div>
    <main class="min-w-0 px-4 md:px-8 py-6">
      <slot />
    </main>
  </div>
</template>
