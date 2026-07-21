<script setup lang="ts">
import { computed } from 'vue'
import { RouterView, useRoute } from 'vue-router'
import AppShell from '@/components/AppShell.vue'

const route = useRoute()
const showShell = computed(() => Boolean(route.meta.requiresAuth))
</script>

<template>
  <AppShell v-if="showShell"><RouterView /></AppShell>
  <RouterView v-else />
</template>
