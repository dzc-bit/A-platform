<script setup lang="ts">
import StatusBadge from '@/components/StatusBadge.vue'
import type { AgentTrace } from '@/types'

withDefaults(defineProps<{
  trace: AgentTrace[]
  /** True while the answer is still streaming; the latest node keeps pulsing. */
  pending?: boolean
}>(), { pending: false })
</script>

<template>
  <ol class="trace-timeline" aria-label="AI 决策过程">
    <li
      v-for="(item, index) in trace"
      :key="`${item.step}-${index}`"
      class="trace-timeline-item"
      :class="[
        `trace-timeline-item--${item.status}`,
        { 'trace-timeline-item--pending': pending && index === trace.length - 1 },
      ]"
    >
      <div class="trace-timeline-body">
        <div class="trace-timeline-head">
          <strong>{{ item.step }}</strong>
          <StatusBadge :value="item.status" type="trace" />
        </div>
        <p v-if="item.detail" class="trace-timeline-detail">{{ item.detail }}</p>
      </div>
    </li>
  </ol>
</template>
