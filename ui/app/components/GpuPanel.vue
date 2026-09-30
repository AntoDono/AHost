<script setup lang="ts">
const props = defineProps<{ gpus: Gpu[], apps: string[] }>()
const maxTotal = computed(() => Math.max(1, ...props.gpus.map(g => g.total_mib)))
const appNames = computed(() => new Set(props.apps))
const NuxtLink = resolveComponent('NuxtLink')
</script>

<template>
    <div class="space-y-4">
      <section v-for="g in gpus" :key="g.uuid" class="rounded-lg border border-default bg-[var(--ah-panel)] p-4">
        <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1 mb-3">
          <h2 class="font-medium text-highlighted">{{ g.name }}</h2>
          <span class="data text-xs text-muted">nvidia-smi {{ g.index }} · /dev/nvidia{{ g.minor }} · bus {{ g.bus }}</span>
          <span class="ml-auto data text-xs text-muted">{{ g.util ?? '–' }}% busy · {{ g.temp ?? '–' }}°C</span>
        </div>
        <div class="h-8 rounded-md border border-default bg-[var(--ah-panel-2)] overflow-hidden flex" :style="{ width: `${(g.total_mib / maxTotal) * 100}%` }" role="img" :aria-label="`${mib(g.used_mib)} of ${mib(g.total_mib)} used`">
          <UTooltip v-for="u in g.users" :key="u.name" :text="`${u.name}: ${mib(u.mib)}`">
            <div class="h-full border-r border-[var(--ah-panel)]" :style="{ width: `${(u.mib / g.total_mib) * 100}%`, background: cableColor(u.name) }" />
          </UTooltip>
        </div>
        <div class="data text-xs text-muted mt-2">{{ mib(g.used_mib) }} of {{ mib(g.total_mib) }} used</div>
        <div class="flex flex-wrap gap-2 mt-3" :aria-label="`Apps using ${g.name}`">
          <component
            :is="appNames.has(u.name) ? NuxtLink : 'span'"
            v-for="u in g.users" :key="u.name"
            v-bind="appNames.has(u.name) ? { to: { path: '/rack', query: { app: u.name } } } : {}"
            class="inline-flex items-center gap-2 rounded-md border pl-1.5 pr-2.5 py-1 text-sm"
            :class="appNames.has(u.name) ? 'hover:bg-[var(--ah-panel-2)] focus-visible:outline-2 focus-visible:outline-primary' : 'border-dashed'"
            :style="{ borderColor: cableColor(u.name) }"
            :title="appNames.has(u.name) ? `Open ${u.name}` : `${u.name} isn't managed by AHost`"
          >
            <span class="size-3 rounded-sm" :style="{ background: cableColor(u.name) }" />
            <span class="font-medium">{{ u.name }}</span>
            <span class="data text-xs text-muted">{{ mib(u.mib) }}</span>
            <span v-if="!appNames.has(u.name)" class="text-xs text-dimmed">not AHost</span>
          </component>
          <span v-if="!g.users.length" class="text-sm text-dimmed">No apps on this card</span>
        </div>
      </section>
    </div>
</template>
