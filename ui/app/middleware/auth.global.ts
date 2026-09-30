export default defineNuxtRouteMiddleware(async (to) => {
  const user = useState<string | null | undefined>('user', () => undefined)
  if (user.value === undefined) {
    try {
      user.value = (await $fetch<{ user: string | null }>('/api/auth/me')).user
    } catch {
      user.value = null
    }
  }
  const isPublic = to.path === '/' || to.path === '/login'
  if (!user.value && !isPublic) return navigateTo('/login')
  if (user.value && to.path === '/login') return navigateTo('/rack')
})
