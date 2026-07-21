<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertCircle, Bell, CheckCircle2, CirclePlus, RefreshCw, Send } from 'lucide-vue-next'
import { errorMessage, streamMyTicketEvents, supportApi } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useAuthStore } from '@/stores/auth'
import type { Ticket, TicketEvent } from '@/types'

type TicketPriority = 'low' | 'normal' | 'high' | 'urgent'

interface TicketRequest {
  customer_name: string
  question: string
  priority: TicketPriority
}

const auth = useAuthStore()
const request = ref<TicketRequest>({ customer_name: '', question: '', priority: 'normal' })
const submitted = ref<Ticket>()
const creating = ref(false)
const error = ref('')
const tickets = ref<Ticket[]>([])
const ticketLoading = ref(false)
const ticketError = ref('')
const selectedTicketId = ref<number>()
const unreadUpdates = ref(0)
let ticketController: AbortController | undefined
let ticketReconnectTimer: ReturnType<typeof window.setTimeout> | undefined
let ticketReconnectAttempt = 0
let ticketViewActive = false

const priorityLabels: Record<TicketPriority, string> = {
  low: '低',
  normal: '普通',
  high: '高',
  urgent: '紧急',
}

watch(
  () => auth.user?.display_name,
  (displayName) => {
    if (displayName && !request.value.customer_name) request.value.customer_name = displayName
  },
  { immediate: true },
)

async function createTicket() {
  creating.value = true
  error.value = ''
  try {
    submitted.value = await supportApi.create(request.value)
    upsertTicket(submitted.value)
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    creating.value = false
  }
}

function submitAnother() {
  submitted.value = undefined
  request.value = {
    customer_name: auth.user?.display_name || request.value.customer_name,
    question: '',
    priority: 'normal',
  }
  error.value = ''
}

const selectedTicket = computed(() => tickets.value.find((ticket) => ticket.id === selectedTicketId.value))

function upsertTicket(ticket: Ticket) {
  const index = tickets.value.findIndex((item) => item.id === ticket.id)
  if (index >= 0) tickets.value.splice(index, 1, ticket)
  else tickets.value.unshift(ticket)
  selectedTicketId.value = ticket.id
}

function receiveTicketEvent(event: TicketEvent) {
  const wasKnown = tickets.value.some((ticket) => ticket.id === event.ticket.id)
  upsertTicket(event.ticket)
  if (wasKnown && submitted.value?.id !== event.ticket.id) unreadUpdates.value += 1
  if (submitted.value?.id === event.ticket.id) submitted.value = event.ticket
}

async function loadMyTickets() {
  ticketLoading.value = true
  ticketError.value = ''
  try {
    tickets.value = await supportApi.mine()
    if (!selectedTicketId.value && tickets.value[0]) selectedTicketId.value = tickets.value[0].id
  } catch (caught) {
    ticketError.value = errorMessage(caught)
  } finally {
    ticketLoading.value = false
  }
}

function clearUnread() {
  unreadUpdates.value = 0
}

function scheduleTicketReconnect() {
  if (!ticketViewActive || ticketReconnectTimer !== undefined) return
  const delay = Math.min(15_000, 800 * (2 ** Math.min(ticketReconnectAttempt, 4)))
  ticketReconnectAttempt += 1
  ticketReconnectTimer = window.setTimeout(() => {
    ticketReconnectTimer = undefined
    connectTicketStream()
  }, delay)
}

function connectTicketStream() {
  if (!ticketViewActive) return
  ticketController?.abort()
  const controller = new AbortController()
  ticketController = controller
  void streamMyTicketEvents(receiveTicketEvent, { signal: controller.signal })
    .then(() => {
      if (!controller.signal.aborted) scheduleTicketReconnect()
    })
    .catch((caught) => {
      if (!controller.signal.aborted) {
        ticketError.value = errorMessage(caught)
        scheduleTicketReconnect()
      }
    })
}

onMounted(async () => {
  ticketViewActive = true
  await loadMyTickets()
  connectTicketStream()
})

onBeforeUnmount(() => {
  ticketViewActive = false
  ticketController?.abort()
  if (ticketReconnectTimer !== undefined) window.clearTimeout(ticketReconnectTimer)
})
</script>

<template>
  <div class="ticket-request-page">
    <section class="page-toolbar">
      <div><p class="eyebrow">服务支持</p><h2>提交客服工单</h2><p>填写需要客服继续跟进的业务问题。</p></div>
    </section>

    <section class="ticket-request-surface" aria-live="polite">
      <form v-if="!submitted" class="ticket-request-form" @submit.prevent="createTicket">
        <div class="section-heading"><div><p class="eyebrow">工单信息</p><h3>业务支持请求</h3></div></div>
        <label class="field-label" for="request-customer">联系人名称</label>
        <input id="request-customer" v-model="request.customer_name" class="field-control" autocomplete="name" required minlength="2" maxlength="80" />

        <label class="field-label" for="request-priority">优先级</label>
        <select id="request-priority" v-model="request.priority" class="field-control">
          <option value="low">低</option>
          <option value="normal">普通</option>
          <option value="high">高</option>
          <option value="urgent">紧急</option>
        </select>

        <label class="field-label" for="request-question">问题描述</label>
        <textarea id="request-question" v-model="request.question" class="field-control ticket-request-question" rows="8" required minlength="5" maxlength="4000" />

        <p v-if="error" class="inline-error" role="alert"><AlertCircle :size="16" />{{ error }}</p>
        <button class="button button--primary ticket-request-submit" :disabled="creating" type="submit">
          <span v-if="creating" class="button-spinner" />
          <Send v-else :size="17" />
          {{ creating ? '正在提交' : '提交工单' }}
        </button>
      </form>

      <div v-else class="ticket-confirmation">
        <span class="ticket-confirmation-icon"><CheckCircle2 :size="30" /></span>
        <div><p class="eyebrow">提交成功</p><h2>工单 #{{ submitted.id }}</h2></div>
        <p class="ticket-confirmation-question">{{ submitted.question }}</p>
        <dl class="ticket-confirmation-meta">
          <div><dt>状态</dt><dd><StatusBadge :value="submitted.status" /></dd></div>
          <div><dt>优先级</dt><dd>{{ priorityLabels[submitted.priority as TicketPriority] ?? submitted.priority }}</dd></div>
          <div><dt>问题分类</dt><dd>{{ submitted.category }}</dd></div>
          <div><dt>提交时间</dt><dd>{{ new Date(submitted.created_at).toLocaleString('zh-CN') }}</dd></div>
        </dl>
        <button class="button button--secondary" type="button" @click="submitAnother"><CirclePlus :size="17" />继续提交</button>
      </div>
    </section>

    <section class="my-tickets-panel" aria-live="polite">
      <header class="section-heading my-tickets-heading">
        <div><p class="eyebrow">实时消息</p><h3>我的工单</h3></div>
        <div class="my-tickets-actions">
          <span v-if="unreadUpdates" class="unread-ticket-count"><Bell :size="14" />{{ unreadUpdates }} 条更新</span>
          <button class="icon-button" type="button" title="刷新我的工单" aria-label="刷新我的工单" :disabled="ticketLoading" @click="loadMyTickets"><RefreshCw :size="16" :class="{ 'is-spinning': ticketLoading }" /></button>
          <button v-if="unreadUpdates" class="button button--quiet" type="button" @click="clearUnread">标记已读</button>
        </div>
      </header>
      <LoadingState v-if="ticketLoading" compact label="正在载入工单" />
      <p v-else-if="ticketError" class="inline-error" role="alert"><AlertCircle :size="15" />{{ ticketError }}</p>
      <div v-else-if="tickets.length" class="my-tickets-content">
        <div class="my-ticket-list">
          <button v-for="ticket in tickets" :key="ticket.id" class="my-ticket-item" :class="{ 'my-ticket-item--active': selectedTicketId === ticket.id }" type="button" @click="selectedTicketId = ticket.id">
            <span><strong>#{{ ticket.id }} {{ ticket.category }}</strong><small>{{ ticket.question }}</small></span><StatusBadge :value="ticket.status" />
          </button>
        </div>
        <div v-if="selectedTicket" class="my-ticket-detail">
          <div class="ticket-meta"><StatusBadge :value="selectedTicket.status" /><span>更新时间 {{ new Date(selectedTicket.updated_at).toLocaleString('zh-CN') }}</span></div>
          <p>{{ selectedTicket.question }}</p>
          <p v-if="selectedTicket.status === 'resolved' && selectedTicket.final_reply" class="my-ticket-reply"><strong>客服回复</strong>{{ selectedTicket.final_reply }}</p>
          <p v-else class="my-ticket-waiting">工单已进入客服队列，状态变化和最终回复会实时推送到这里。</p>
        </div>
      </div>
      <EmptyState v-else title="暂无我的工单" description="提交第一个客服问题后，状态和回复会在此持续更新。" />
    </section>
  </div>
</template>
