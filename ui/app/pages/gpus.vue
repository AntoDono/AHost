<script setup lang="ts">
const { data } = useOverview()
const gpus = computed(() => data.value?.gpus ?? [])
const maxTotal = computed(() => Math.max(1, ...gpus.value.map(g => g.total_mib)))
const appNames = computed(() => new Set((data.value?.apps ?? []).map(a => a.name)))
</script>

<template>
  <div class="max-w-6xl mx-auto space-y-6">
    <header>
      <h1 class="silk text-3xl text-highlighted">GPUs</h1>
      <p class="text-sm text-muted mt-1">Bars are drawn to scale by memory. Each segment is one app or service using that card.</p>
    </header>
    <p v-if="data && !gpus.length" class="text-muted">No NVIDIA GPUs found.</p>
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
        <div class="flex flex-wrap items-center gap-x-4 gap-y-1 mt-2 text-sm">
          <span class="data text-xs text-muted">{{ mib(g.used_mib) }} / {{ mib(g.total_mib) }}</span>
          <span v-for="u in g.users" :key="u.name" class="flex items-center gap-1.5 text-xs">
            <span class="size-2.5 rounded-sm" :style="{ background: cableColor(u.name) }" />
            {{ u.name }}<span class="text-dimmed" v-if="!appNames.has(u.name)"> (not AHost)</span>
            <span class="data text-xs text-muted">{{ mib(u.mib) }}</span>
          </span>
          <span v-if="!g.users.length" class="text-xs text-dimmed">idle</span>
        </div>
      </section>
    </div>
  </div>
</template>
