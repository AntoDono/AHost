<script setup lang="ts">
definePageMeta({ layout: 'bare' })
const username = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
const user = useState<string | null | undefined>('user')
const api = useApi()

async function submit() {
  error.value = ''
  busy.value = true
  try {
    const r = await api<{ user: string }>('/api/auth/login', { method: 'POST', body: { username: username.value, password: password.value } })
    user.value = r.user
    await navigateTo('/rack')
  } catch (e) {
    error.value = apiError(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="w-full max-w-sm rounded-xl border border-default bg-[var(--ah-panel)] p-7 shadow-sm" @submit.prevent="submit">
    <div class="flex items-center justify-between mb-7">
      <span class="silk text-2xl tracking-[0.14em]">AHost</span>
      <span class="flex gap-1.5" aria-hidden="true"><span class="led" data-state="running" /><span class="led" /><span class="led" /></span>
    </div>
    <div class="space-y-4">
      <UFormField label="Username">
        <UInput v-model="username" autocomplete="username" autofocus required class="w-full" />
      </UFormField>
      <UFormField label="Password">
        <UInput v-model="password" type="password" autocomplete="current-password" required class="w-full" />
      </UFormField>
      <p v-if="error" class="text-sm text-[var(--ah-fault)]" role="alert">{{ error }}</p>
      <UButton type="submit" block :loading="busy" label="Sign in" />
    </div>
  </form>
</template>
