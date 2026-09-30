<script setup lang="ts">
// The GPU view lives on the System page now.
definePageMeta({ middleware: () => navigateTo('/system', { replace: true }) })
</script>

<template><div /></template>
