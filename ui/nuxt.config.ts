// AHost dashboard: a static SPA served by the AHost API (same origin, cookie auth).
export default defineNuxtConfig({
  compatibilityDate: '2026-09-01',
  ssr: false,
  modules: ['@nuxt/ui'],
  css: ['~/assets/css/main.css'],
  devtools: { enabled: false },
  app: {
    head: {
      title: 'AHost',
      meta: [{ name: 'robots', content: 'noindex' }],
    },
  },
  icon: {
    // bundle the icons we use so the dashboard never fetches from a CDN (and CSP stays 'self')
    serverBundle: false,
    clientBundle: { scan: true, sizeLimitKb: 512 },
    provider: 'none',
  },
  fonts: {
    families: [
      { name: 'Barlow Condensed', provider: 'google', weights: [500, 600, 700] },
      { name: 'Instrument Sans', provider: 'google', weights: [400, 500, 600] },
      { name: 'IBM Plex Mono', provider: 'google', weights: [400, 500] },
    ],
  },
  nitro: { prerender: { routes: ['/'] } },
  vite: {
    server: { proxy: { '/api': { target: 'http://127.0.0.1:9900', changeOrigin: false } } },
  },
})
