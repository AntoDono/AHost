<script setup lang="ts">
const props = defineProps<{ app: AppInfo, busy?: string | null, gpus: Gpu[] }>()
const emit = defineEmits<{ open: [tab?: string], action: [action: 'start' | 'stop' | 'restart'] }>()

const main = computed(() => props.app.processes.find(p => p.port) ?? props.app.processes[0])
const cable = computed(() => cableColor(props.app.name))
const running = computed(() => ['running', 'unhealthy', 'starting'].includes(props.app.state))
const memory = computed(() => props.app.processes.reduce((s, p) => s + (p.memory ?? 0), 0))
const restarts = computed(() => props.app.processes.reduce((s, p) => s + p.restarts, 0))
const health = computed(() => main.value?.health)
const unitLabel = computed(() => props.app.multi ? `ahost@${props.app.name} ×${props.app.processes.length}` : main.value?.unit.replace('.service', ''))
const gpuNames = computed(() => props.app.gpus.map(i => props.gpus.find(g => g.index === i)?.name ?? `GPU ${i}`))
const hovered = ref(false)
const lit = computed(() => hovered.value)

// Click anywhere on the strip opens the app, except on its own buttons/links (those do their own thing).
function onClick(e: MouseEvent) {
  if ((e.target as HTMLElement).closest('button, a, [role="button"]')) return
  if (window.getSelection()?.toString()) return // let people select text (domains, ports) without opening
  emit('open')
}
</script>

<template>
  <article
    class="group relative cursor-pointer rounded-lg border border-default bg-[var(--ah-panel)] px-3 py-3 md:px-4 transition-[background-color,border-color,box-shadow] duration-150 hover:bg-[var(--ui-bg-elevated)] hover:border-[color-mix(in_srgb,var(--cable)_45%,var(--ah-rule))] hover:shadow-sm focus-visible:outline-2 focus-visible:outline-primary"
    :class="{ lit }"
    :style="{ '--cable': cable }"
    tabindex="0"
    :aria-label="`${app.name}: ${STATE_LABEL[app.state]}`"
    @mouseenter="hovered = true" @mouseleave="hovered = false" @focusin="hovered = true" @focusout="hovered = false"
    @click="onClick"
    @keydown.l.exact="emit('open', 'logs')" @keydown.enter.exact.self="emit('open')"
  >
    <div class="grid gap-3 lg:grid-cols-[minmax(10rem,14rem)_minmax(0,1fr)_auto] lg:items-center">
      <!-- identity -->
      <button class="flex items-center gap-3 min-w-0 text-left cursor-pointer" @click="emit('open')">
        <span class="led" :data-state="app.state" :class="{ pulsing: busy }" />
        <span class="min-w-0">
          <span class="block font-medium text-highlighted truncate">{{ app.name }}</span>
          <span class="block text-xs text-muted truncate">{{ STATE_LABEL[app.state] }}<template v-if="app.description"> · {{ app.description }}</template></span>
        </span>
      </button>

      <!-- routing strip: domain → port → service → GPU -->
      <div class="route flex flex-wrap items-center gap-1.5 min-w-0 py-1 lg:grid lg:flex-nowrap" aria-label="Request path">
        <span class="jack truncate min-w-0" :title="app.domains.join(', ')">
          <template v-if="app.domains.length">{{ app.domains[0] }}<span v-if="app.domains.length > 1" class="text-dimmed"> +{{ app.domains.length - 1 }}</span></template>
          <span v-else class="text-dimmed">no domain</span>
        </span>
        <span class="cable" aria-hidden="true" />
        <span v-if="main?.port" class="jack port text-center">:{{ main.port }}</span>
        <span v-else class="jack text-center text-dimmed">–</span>
        <span class="cable" aria-hidden="true" />
        <span class="jack text-muted truncate min-w-0" :title="unitLabel">{{ unitLabel }}</span>
        <span class="cable" :class="{ 'opacity-0': !gpuNames.length }" aria-hidden="true" />
        <span class="min-w-0">
          <UTooltip v-if="gpuNames.length" :text="gpuNames.join(' · ')">
            <span class="jack !border-[var(--ah-signal)]/60 inline-block">{{ gpuNames.length === 1 ? gpuNames[0] : `${gpuNames.length} GPUs` }}</span>
          </UTooltip>
        </span>
      </div>

      <!-- vitals + controls -->
      <div class="flex items-center gap-3 justify-between lg:justify-end">
        <div class="flex items-center gap-3 data text-xs text-muted">
          <UTooltip v-if="health" :text="health.ok ? `GET ${health.path} → ${health.status}` : (health.detail || `GET ${health.path} → ${health.status}`)">
            <span :class="health.ok ? '' : 'text-[var(--ah-fault)]'">{{ health.ok ? `${health.ms} ms` : 'no reply' }}</span>
          </UTooltip>
          <span v-if="running">{{ bytes(memory) }}</span>
          <span v-if="running" class="hidden sm:inline">{{ since(main?.since) }}</span>
          <UTooltip v-if="restarts" :text="`${restarts} automatic restart(s) since it started`">
            <span class="text-[var(--ah-signal)]">↻{{ restarts }}</span>
          </UTooltip>
        </div>
        <div class="flex items-center gap-0.5">
          <UTooltip v-if="!running" text="Start">
            <UButton size="sm" variant="ghost" color="neutral" icon="i-lucide-play" aria-label="Start" :loading="busy === 'start'" :disabled="!!busy" @click="emit('action', 'start')" />
          </UTooltip>
          <UTooltip v-if="running" text="Restart">
            <UButton size="sm" variant="ghost" color="neutral" icon="i-lucide-rotate-cw" aria-label="Restart" :loading="busy === 'restart'" :disabled="!!busy" @click="emit('action', 'restart')" />
          </UTooltip>
          <UTooltip v-if="running" text="Stop">
            <UButton size="sm" variant="ghost" color="neutral" icon="i-lucide-square" aria-label="Stop" :loading="busy === 'stop'" :disabled="!!busy" @click="emit('action', 'stop')" />
          </UTooltip>
          <UTooltip text="Logs" :kbds="['L']">
            <UButton size="sm" variant="ghost" color="neutral" icon="i-lucide-scroll-text" aria-label="Logs" @click="emit('open', 'logs')" />
          </UTooltip>
        </div>
      </div>
    </div>
  </article>
</template>

<style scoped>
@media (min-width: 64rem) {
  .route { grid-template-columns: minmax(6rem, 16rem) 1.75rem 4.75rem 1.75rem minmax(6rem, 13rem) 1.75rem auto; }
}
.route .cable { flex: 0 0 1.25rem; }
@media (min-width: 64rem) { .route .cable { width: 100%; } }
</style>
