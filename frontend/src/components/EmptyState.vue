<script setup lang="ts">
import { Inbox } from 'lucide-vue-next'

withDefaults(defineProps<{ title: string; description: string }>(), {})
</script>

<template>
  <div class="empty-state">
    <span class="empty-icon"><Inbox :size="24" /></span>
    <strong>{{ title }}</strong>
    <p>{{ description }}</p>
    <slot />
  </div>
</template>
