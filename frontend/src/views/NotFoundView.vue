<script setup lang="ts">
import { ArrowLeft, MapPinOff } from 'lucide-vue-next'
</script>

<template>
  <main class="not-found-page"><MapPinOff :size="38" /><p class="eyebrow">页面未找到</p><h1>这里没有可访问的工作台。</h1><RouterLink class="button button--primary" to="/chat"><ArrowLeft :size="17" />返回智能对话</RouterLink></main>
</template>
