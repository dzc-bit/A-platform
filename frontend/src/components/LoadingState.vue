<script setup lang="ts">
withDefaults(defineProps<{ label?: string; compact?: boolean }>(), { label: '正在获取数据', compact: false })
</script>

<template>
  <div class="loading-state" :class="{ 'loading-state--compact': compact }" role="status">
    <span class="spinner" aria-hidden="true" />
    <span>{{ label }}</span>
  </div>
</template>
