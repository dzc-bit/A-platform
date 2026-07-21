<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ value: string; type?: 'status' | 'priority' | 'trace' }>()

const label = computed(() => {
  const labels: Record<string, string> = {
    open: '待处理',
    in_progress: '处理中',
    resolved: '已解决',
    low: '低',
    normal: '普通',
    high: '高',
    urgent: '紧急',
    completed: '已完成',
    skipped: '已跳过',
    fallback: '降级处理',
    ready: '可检索',
  }
  return labels[props.value] ?? props.value
})

const tone = computed(() => `badge--${props.value}`)
</script>

<template><span class="badge" :class="tone">{{ label }}</span></template>
