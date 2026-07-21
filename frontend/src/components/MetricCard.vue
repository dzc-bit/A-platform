<script setup lang="ts">
import type { Component } from 'vue'

const props = withDefaults(defineProps<{
  label: string
  value: string | number
  detail: string
  icon: Component
  tone?: 'teal' | 'coral' | 'blue' | 'gold'
  interactive?: boolean
  ariaLabel?: string
}>(), { interactive: false, ariaLabel: '' })
const emit = defineEmits<{ click: [] }>()

function handleClick() {
  if (props.interactive) emit('click')
}
</script>

<template>
  <component
    :is="interactive ? 'button' : 'article'"
    class="metric-card"
    :class="[`metric-card--${tone ?? 'teal'}`, { 'metric-card--interactive': interactive }]"
    :type="interactive ? 'button' : undefined"
    :aria-label="interactive ? (ariaLabel || label) : undefined"
    @click="handleClick"
  >
    <div><p>{{ label }}</p><strong>{{ value }}</strong><small>{{ detail }}</small></div>
    <span class="metric-icon"><component :is="icon" :size="20" /></span>
  </component>
</template>
