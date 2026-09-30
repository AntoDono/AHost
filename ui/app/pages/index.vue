<script setup lang="ts">
// Public landing page. It's on the open internet, so it must not show anything about this server:
// the routing strip below uses example data only.
definePageMeta({ layout: false })
useHead({ title: 'AHost' })
const user = useState<string | null | undefined>('user')

const demo = [
  { domain: 'api.example.com', port: 10003, unit: 'ahost@api', gpu: '' },
  { domain: 'chat.example.com', port: 10007, unit: 'ahost@chat', gpu: 'RTX 3090' },
  { domain: 'docs.example.com', port: 0, unit: 'static files', gpu: '' },
]
const colors = ['#2d6bd8', '#e8a400', '#1f9e8f']
</script>

<template>
  <div class="min-h-dvh flex flex-col bg-[var(--ah-ground)]">
    <header class="flex items-center justify-between px-5 md:px-10 py-5">
      <span class="flex items-center gap-2">
        <span class="grid grid-cols-2 gap-[3px]" aria-hidden="true"><span class="screw" /><span class="screw" /><span class="screw" /><span class="screw" /></span>
        <span class="silk text-lg tracking-[0.14em]">AHost</span>
      </span>
      <UButton v-if="user" to="/rack" icon="i-lucide-server" label="Open the rack" />
      <UButton v-else to="/login" color="neutral" variant="outline" icon="i-lucide-log-in" label="Sign in" />
    </header>

    <main class="flex-1 px-5 md:px-10">
      <section class="max-w-5xl mx-auto pt-10 md:pt-20 pb-14">
        <h1 class="silk text-[clamp(3rem,11vw,8.5rem)] leading-[0.85] text-highlighted">AHost</h1>
        <p class="mt-6 max-w-xl text-lg md:text-xl text-muted">
          Every app on this server in one rack: which domain goes to which port, which service answers it,
          and which GPU it's using.
        </p>

        <!-- the thesis: a request's path, drawn as patch cables -->
        <div class="mt-12 space-y-2.5" aria-hidden="true">
          <div
            v-for="(d, i) in demo" :key="d.domain"
            class="lit strip rounded-lg border border-default bg-[var(--ah-panel)] px-4 py-3 flex flex-wrap items-center gap-2"
            :style="{ '--cable': colors[i], '--delay': `${i * 0.9}s` }"
          >
            <span class="led" data-state="running" />
            <span class="jack">{{ d.domain }}</span>
            <span class="cable" />
            <span class="jack port">{{ d.port ? `:${d.port}` : 'nginx' }}</span>
            <span class="cable" />
            <span class="jack text-muted">{{ d.unit }}</span>
            <template v-if="d.gpu">
              <span class="cable" />
              <span class="jack !border-[var(--ah-signal)]/60">{{ d.gpu }}</span>
            </template>
          </div>
        </div>
      </section>

      <section class="max-w-5xl mx-auto grid gap-8 md:grid-cols-3 pb-20 border-t border-default pt-10">
        <div>
          <h2 class="font-medium text-highlighted">Describe an app once</h2>
          <p class="mt-2 text-sm text-muted">A folder, a start command, a domain. AHost picks the port and writes the service, the web site and the certificate.</p>
        </div>
        <div>
          <h2 class="font-medium text-highlighted">See what's running</h2>
          <p class="mt-2 text-sm text-muted">Live status, health checks and logs for every app, plus the load on each CPU thread and GPU.</p>
        </div>
        <div>
          <h2 class="font-medium text-highlighted">Change it safely</h2>
          <p class="mt-2 text-sm text-muted">Every change is checked and previewed first. nginx is tested before it reloads, and a failed step is undone.</p>
        </div>
      </section>
    </main>

    <footer class="px-5 md:px-10 py-6 text-xs text-dimmed flex justify-between">
      <span>Private dashboard. Access is by account only.</span>
      <NuxtLink v-if="!user" to="/login" class="hover:text-muted">Sign in</NuxtLink>
    </footer>
  </div>
</template>

<style scoped>
/* cables light up one after another, like a request travelling the path */
.strip .cable, .strip .cable::after, .strip .jack.port {
  animation: travel 3.6s ease-in-out infinite;
  animation-delay: var(--delay);
}
@keyframes travel {
  0%, 100% { filter: saturate(0.25) opacity(0.55); }
  20%, 45% { filter: none; }
}
@media (prefers-reduced-motion: reduce) {
  .strip .cable, .strip .cable::after, .strip .jack.port { animation: none; }
}
.strip .cable { flex: 0 1 3rem; }
</style>
