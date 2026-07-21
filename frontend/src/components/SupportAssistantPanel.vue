<script setup lang="ts">
import { ref, watch } from 'vue'
import { BookOpen, Bot, ChevronDown, CircleAlert, RotateCcw, Send, Sparkles } from 'lucide-vue-next'
import { errorMessage, supportApi } from '@/api/client'
import LoadingState from '@/components/LoadingState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import type { AgentTrace, Citation, SupportAssistantResponse } from '@/types'

const props = withDefaults(defineProps<{
  conversationId?: number
  customerName?: string | null
}>(), {
  conversationId: undefined,
  customerName: null,
})

const prompt = ref('')
const useKnowledge = ref(false)
const loading = ref(false)
const error = ref('')
const response = ref<SupportAssistantResponse | null>(null)

function reset() {
  prompt.value = ''
  response.value = null
  error.value = ''
}

function traceLabel(trace: AgentTrace) {
  return trace.status === 'fallback' ? '降级' : trace.status === 'skipped' ? '跳过' : '完成'
}

function citationLabel(citation: Citation) {
  return `${citation.title} · ${Math.round(citation.score * 100)}%`
}

async function ask() {
  const message = prompt.value.trim()
  if (!message || loading.value) return
  loading.value = true
  error.value = ''
  try {
    response.value = await supportApi.assistant({
      query: message,
      ...(props.conversationId ? { conversation_id: props.conversationId } : {}),
      // The support copilot is a general chat model by default. Knowledge is
      // an optional signal, never the only source of the recommendation.
      use_knowledge: useKnowledge.value,
    })
    prompt.value = ''
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

watch(() => props.conversationId, () => {
  // A recommendation belongs to the selected customer session. Do not leak
  // the previous session's answer when an agent moves through the queue.
  reset()
})
</script>

<template>
  <section class="support-assistant-panel" aria-label="客服辅助 AI">
    <header class="support-assistant-header">
      <div class="support-assistant-title">
        <span class="support-assistant-icon"><Bot :size="18" /></span>
        <div>
          <p class="eyebrow">客服专属工作区</p>
          <strong>客服辅助 AI</strong>
          <small>{{ customerName ? `当前会话：${customerName}` : '可先提出通用业务问题' }}</small>
        </div>
      </div>
      <div class="support-assistant-model">
        <Sparkles :size="14" />
        <span>通用模型</span>
        <small>{{ response?.model_mode || response?.model || '非强制知识库' }}</small>
      </div>
    </header>

    <div class="support-assistant-policy">
      <span><Sparkles :size="14" />回答会结合上下文与业务推理</span>
      <label class="support-assistant-knowledge-toggle">
        <input v-model="useKnowledge" type="checkbox" />
        <BookOpen :size="14" />
        <span>需要时参考知识库</span>
      </label>
    </div>

    <div v-if="response" class="support-assistant-result" aria-live="polite">
      <div class="support-assistant-result-heading">
        <strong>辅助建议</strong>
        <button class="icon-button" type="button" title="清空辅助建议" aria-label="清空辅助建议" @click="reset"><RotateCcw :size="15" /></button>
      </div>
      <p class="support-assistant-answer">{{ response.answer }}</p>
      <details v-if="response.citations?.length" class="support-assistant-details">
        <summary><BookOpen :size="14" />知识依据（可选）<ChevronDown :size="14" /></summary>
        <div class="support-assistant-citations">
          <article v-for="citation in response.citations" :key="`${citation.document_id}-${citation.title}`">
            <strong>{{ citationLabel(citation) }}</strong>
            <p>{{ citation.excerpt }}</p>
          </article>
        </div>
      </details>
      <details v-if="response.trace?.length" class="support-assistant-details">
        <summary><Sparkles :size="14" />处理轨迹<ChevronDown :size="14" /></summary>
        <div class="support-assistant-trace">
          <div v-for="(trace, index) in response.trace" :key="`${trace.step}-${index}`">
            <strong>{{ trace.step }}</strong><StatusBadge :value="traceLabel(trace)" type="trace" /><span>{{ trace.detail }}</span>
          </div>
        </div>
      </details>
    </div>
    <p v-else class="support-assistant-empty">输入客户问题、业务规则或回复语气要求，AI 只为客服提供建议，不会直接发送给客户。</p>

    <form class="support-assistant-composer" @submit.prevent="ask">
      <textarea v-model="prompt" rows="3" maxlength="4000" :disabled="loading" placeholder="例如：帮我把当前问题整理成一段专业、简洁的回复" @keydown.enter.exact.prevent="ask" />
      <div class="support-assistant-composer-footer">
        <small>{{ useKnowledge ? '本次会参考知识库，并保留通用推理' : '本次不强制检索知识库' }}</small>
        <button class="button button--primary" type="submit" :disabled="loading || !prompt.trim()"><Send :size="15" />{{ loading ? '生成中' : '生成辅助建议' }}</button>
      </div>
    </form>
    <LoadingState v-if="loading" compact label="客服辅助 AI 正在生成建议" />
    <p v-if="error" class="inline-error"><CircleAlert :size="14" />{{ error }}</p>
  </section>
</template>
