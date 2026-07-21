<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertCircle, BookOpen, Check, CirclePlus, Filter, MessageCircle, RefreshCw, Save, Send, Sparkles, X } from 'lucide-vue-next'
import { errorMessage, knowledgeApi, realtimeApi, streamSupportConversationEvents, streamSupportNotificationEvents, streamSupportTicketEvents, supportApi } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import RealtimeChatPanel from '@/components/RealtimeChatPanel.vue'
import SupportAssistantPanel from '@/components/SupportAssistantPanel.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import type { Citation, Conversation, ConversationEvent, ConversationMessage, SupportNotification, Ticket, TicketEvent } from '@/types'

const tickets = ref<Ticket[]>([])
const selectedId = ref<number>()
const filter = ref('pending')
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const reply = ref('')
const ticketBasis = ref<Citation[]>([])
const basisLoading = ref(false)
const basisError = ref('')
const showNew = ref(false)
const newTicket = ref({ customer_name: '', question: '', priority: 'normal' })
const creating = ref(false)
const createError = ref('')
const supportConversations = ref<Conversation[]>([])
const selectedConversationId = ref<number>()
const conversationScope = ref<'pending' | 'all'>('pending')
const detailMode = ref<'ticket' | 'conversation'>('ticket')
const conversationLoading = ref(false)
const conversationError = ref('')
const conversationPreviews = ref<Record<number, ConversationMessage[]>>({})
const showKnowledge = ref(false)
const notifications = ref<SupportNotification[]>([])
let liveController: AbortController | undefined
let conversationController: AbortController | undefined
let notificationController: AbortController | undefined
const pendingConversationHydrates = new Set<number>()

const visibleTickets = computed(() => {
  if (filter.value === 'all') return tickets.value
  if (filter.value === 'pending') return tickets.value.filter((ticket) => ticket.status !== 'resolved')
  return tickets.value.filter((ticket) => ticket.status === filter.value)
})
const selectedTicket = computed(() => tickets.value.find((ticket) => ticket.id === selectedId.value))
const selectedConversation = computed(() => supportConversations.value.find((conversation) => conversation.id === selectedConversationId.value))
const openCount = computed(() => tickets.value.filter((ticket) => ticket.status !== 'resolved').length)

const visibleConversations = computed(() => conversationScope.value === 'all'
  ? supportConversations.value
  : supportConversations.value.filter((conversation) => conversationStatus(conversation) !== 'closed'))

function linkedTicket(conversation: Conversation) {
  if (conversation.related_ticket) return conversation.related_ticket
  const ticketId = conversation.ticket_id ?? conversation.related_ticket_id
  if (ticketId) return tickets.value.find((ticket) => ticket.id === ticketId)
  return tickets.value.find((ticket) => (ticket as Ticket & { conversation_id?: number | null }).conversation_id === conversation.id)
}

function conversationCustomer(conversation: Conversation) {
  return conversation.customer_name || conversation.customer_display_name || conversation.customer?.display_name || conversation.customer?.name || `企业用户 #${conversation.user_id ?? conversation.customer_id ?? conversation.customer?.id ?? conversation.id}`
}

function conversationPriority(conversation: Conversation) {
  return conversation.priority || linkedTicket(conversation)?.priority || 'normal'
}

function conversationUnread(conversation: Conversation) {
  if (typeof conversation.unread_count === 'number') return conversation.unread_count
  const preview = conversationPreviews.value[conversation.id] ?? []
  return preview.filter((message) => message.role === 'user').length
}

function conversationRecentMessage(conversation: Conversation) {
  const recent = conversation.recent_message || conversation.last_message
  if (recent) return recent.content
  const preview = conversationPreviews.value[conversation.id] ?? []
  return preview[preview.length - 1]?.content || '等待最近消息'
}

function conversationStatus(conversation?: Conversation) {
  return conversation?.handoff_status || conversation?.status || 'requested'
}

function conversationAssignedLabel(conversation: Conversation) {
  return conversation.assigned_agent?.display_name || (conversation.assigned_agent_id ? `客服 #${conversation.assigned_agent_id}` : '未分配')
}

const selectedConversationPriority = computed(() => selectedConversation.value ? conversationPriority(selectedConversation.value) : null)
const unreadNotifications = computed(() => notifications.value.length)

watch(filter, (nextFilter) => {
  const current = selectedTicket.value
  const matches = (ticket: Ticket) => nextFilter === 'all' || (nextFilter === 'pending' ? ticket.status !== 'resolved' : ticket.status === nextFilter)
  if (current && matches(current)) return
  const first = tickets.value.find(matches)
  if (first) choose(first)
})

function choose(ticket: Ticket) {
  selectedId.value = ticket.id
  detailMode.value = 'ticket'
  reply.value = ticket.final_reply || ticket.suggested_reply
  void loadTicketBasis(ticket)
}

async function loadTicketBasis(ticket: Ticket) {
  const ticketId = ticket.id
  basisLoading.value = true
  basisError.value = ''
  ticketBasis.value = []
  try {
    const results = (await knowledgeApi.search({ query: ticket.question, top_k: 3 })).results
    if (selectedId.value === ticketId) ticketBasis.value = results
  } catch (caught) {
    if (selectedId.value === ticketId) basisError.value = errorMessage(caught)
  } finally {
    if (selectedId.value === ticketId) basisLoading.value = false
  }
}

function chooseConversation(conversation: Conversation) {
  selectedConversationId.value = conversation.id
  detailMode.value = 'conversation'
  conversation.unread_count = 0
  if (conversationStatus(conversation) !== 'closed') {
    void realtimeApi.markConversationRead(conversation.id).then((updated) => updateConversation(updated)).catch(() => undefined)
  }
}

function receiveNotification(event: ConversationEvent) {
  const incoming = event.notification ?? (
    event.action === 'notification' || event.action === 'assignment'
      ? {
          conversation_id: event.conversation_id,
          agent_id: event.agent_id ?? event.assigned_agent_id,
          message: event.notice || event.takeover_notice || event.message?.content || '管理者更新了当前会话分配。',
          notice: event.notice || event.takeover_notice,
          created_at: event.message?.created_at ?? new Date().toISOString(),
          kind: event.action,
        }
      : null
  )
  if (!incoming) return
  const text = incoming.message || incoming.content || incoming.notice
  if (!text) return
  const key = incoming.id ?? `${incoming.conversation_id ?? event.conversation_id}-${incoming.created_at}-${text}`
  if (notifications.value.some((item) => (item.id ?? `${item.conversation_id}-${item.created_at}-${item.message}`) === key)) return
  notifications.value = [{ ...incoming, message: text }, ...notifications.value].slice(0, 12)
  const conversation = supportConversations.value.find((item) => item.id === event.conversation_id)
  if (conversation) {
    conversation.takeover_notice = incoming.notice || text
    conversation.last_notification = { ...incoming, message: text }
  }
}

function upsertTicket(ticket: Ticket, prepend = false) {
  const index = tickets.value.findIndex((item) => item.id === ticket.id)
  if (index >= 0) tickets.value.splice(index, 1, ticket)
  else if (prepend) tickets.value.unshift(ticket)
  else tickets.value.push(ticket)
}

function receiveTicketEvent(event: TicketEvent) {
  upsertTicket(event.ticket, event.action === 'created')
}

function receiveConversationEvent(event: ConversationEvent) {
  if (event.action === 'notification' || event.action === 'assignment' || event.action === 'executive_takeover' || event.notification || event.notice || event.takeover_notice) {
    receiveNotification(event)
  }
  const existing = supportConversations.value.find((item) => item.id === event.conversation_id)
  if (existing) {
    if (event.status) existing.handoff_status = event.status
    if (event.assigned_agent_id !== undefined) existing.assigned_agent_id = event.assigned_agent_id
    if (event.takeover_by_id !== undefined) {
      existing.takeover_by_id = event.takeover_by_id
      existing.control_mode = event.takeover_by_id ? 'executive_takeover' : 'support_agent'
    }
    if (event.notice || event.takeover_notice) existing.takeover_notice = event.notice || event.takeover_notice
    if (event.notification) existing.last_notification = event.notification
    existing.status = event.status ?? existing.status
    existing.updated_at = event.message?.created_at ?? new Date().toISOString()
    if (event.message) {
      const current = conversationPreviews.value[event.conversation_id] ?? []
      const isNewMessage = !current.some((item) => item.id === event.message?.id)
      if (isNewMessage) {
        conversationPreviews.value[event.conversation_id] = [...current, event.message]
        existing.recent_message = event.message
        existing.last_message = event.message
        if (event.message.role === 'user') {
          existing.unread_count = selectedConversationId.value === event.conversation_id
            ? 0
            : (existing.unread_count ?? 0) + 1
        } else if (event.message.role === 'agent' || event.message.role === 'support_agent' || event.message.role === 'system') {
          existing.unread_count = 0
        }
      }
    }
    supportConversations.value.sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    return
  }
  if (event.action === 'handoff' || event.action === 'assignment' || event.action === 'notification' || event.action === 'created' || event.action === 'executive_takeover' || event.notification) {
    void hydrateConversation(event.conversation_id)
  }
}

async function loadNotifications() {
  try {
    const result = await supportApi.notifications(true)
    notifications.value = result.map((notification) => ({
      ...notification,
      message: notification.message || notification.content || '',
    }))
  } catch {
    // The live notification stream remains available when a legacy server
    // does not expose the persisted notification list.
  }
}

async function clearNotifications() {
  const current = notifications.value
  notifications.value = []
  await Promise.all(current.map((notification) => (
    typeof notification.id === 'number'
      ? supportApi.markNotificationRead(notification.id).catch(() => undefined)
      : Promise.resolve(undefined)
  )))
}

async function hydrateConversation(conversationId: number) {
  if (pendingConversationHydrates.has(conversationId)) return
  pendingConversationHydrates.add(conversationId)
  try {
    const conversation = await realtimeApi.supportConversation(conversationId)
    const index = supportConversations.value.findIndex((item) => item.id === conversation.id)
    if (index >= 0) supportConversations.value.splice(index, 1, { ...supportConversations.value[index], ...conversation })
    else supportConversations.value.unshift(conversation)
    conversationPreviews.value[conversation.id] = await realtimeApi.supportMessages(conversation.id)
    supportConversations.value.sort((left, right) => right.updated_at.localeCompare(left.updated_at))
    if (!selectedConversationId.value) selectedConversationId.value = conversation.id
  } catch {
    // The event stream remains usable if a newly-created row disappears before hydration.
  } finally {
    pendingConversationHydrates.delete(conversationId)
  }
}

async function loadTickets() {
  loading.value = true
  error.value = ''
  try {
    tickets.value = await supportApi.tickets()
    const current = tickets.value.find((ticket) => ticket.id === selectedId.value)
    const matches = (ticket: Ticket) => filter.value === 'all' || (filter.value === 'pending' ? ticket.status !== 'resolved' : ticket.status === filter.value)
    const firstVisible = tickets.value.find(matches)
    if (current && matches(current)) choose(current)
    else if (firstVisible) choose(firstVisible)
    else if (filter.value === 'all' && tickets.value[0]) choose(tickets.value[0])
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

async function loadSupportConversations() {
  conversationLoading.value = true
  conversationError.value = ''
  try {
    supportConversations.value = await realtimeApi.supportConversations(conversationScope.value)
    conversationPreviews.value = {}
    await Promise.all(supportConversations.value.map(async (conversation) => {
      try {
        conversationPreviews.value[conversation.id] = await realtimeApi.supportMessages(conversation.id)
      } catch {
        // The selected conversation still loads in the detail panel if a preview fails.
      }
    }))
    if (!selectedConversationId.value && visibleConversations.value[0]) selectedConversationId.value = visibleConversations.value[0].id
  } catch (caught) {
    conversationError.value = errorMessage(caught)
  } finally {
    conversationLoading.value = false
  }
}

function changeConversationScope(scope: 'pending' | 'all') {
  if (conversationScope.value === scope) return
  conversationScope.value = scope
  selectedConversationId.value = undefined
  detailMode.value = 'ticket'
  void loadSupportConversations()
}

function updateConversation(updated: Conversation) {
  const index = supportConversations.value.findIndex((conversation) => conversation.id === updated.id)
  if (index >= 0) supportConversations.value.splice(index, 1, { ...supportConversations.value[index], ...updated })
}

function closeConversation(conversationId: number) {
  const index = supportConversations.value.findIndex((conversation) => conversation.id === conversationId)
  if (index >= 0) supportConversations.value[index].handoff_status = 'closed'
  if (selectedConversationId.value === conversationId) {
    selectedConversationId.value = visibleConversations.value.find((conversation) => conversation.id !== conversationId)?.id
    detailMode.value = selectedConversationId.value ? 'conversation' : 'ticket'
  }
}

async function updateTicket(status?: string) {
  const current = selectedTicket.value
  if (!current) return
  saving.value = true
  error.value = ''
  try {
    const result = await supportApi.update(current.id, { status: status ?? current.status, final_reply: reply.value.trim() || undefined })
    const index = tickets.value.findIndex((ticket) => ticket.id === result.id)
    if (index >= 0) tickets.value.splice(index, 1, result)
    else upsertTicket(result, true)
    choose(result)
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    saving.value = false
  }
}

function handleStatusChange(event: Event) {
  void updateTicket((event.target as HTMLSelectElement).value)
}

async function createTicket() {
  creating.value = true
  createError.value = ''
  try {
    const result = await supportApi.create(newTicket.value)
    upsertTicket(result, true)
    choose(result)
    newTicket.value = { customer_name: '', question: '', priority: 'normal' }
    showNew.value = false
  } catch (caught) {
    createError.value = errorMessage(caught)
  } finally {
    creating.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadTickets(), loadSupportConversations(), loadNotifications()])
  liveController = new AbortController()
  void streamSupportTicketEvents(receiveTicketEvent, { signal: liveController.signal }).catch((caught) => {
    if (!liveController?.signal.aborted) error.value = errorMessage(caught)
  })
  conversationController = new AbortController()
  void streamSupportConversationEvents(receiveConversationEvent, { signal: conversationController.signal }).catch((caught) => {
    if (!conversationController?.signal.aborted) conversationError.value = errorMessage(caught)
  })
  notificationController = new AbortController()
  void streamSupportNotificationEvents(receiveConversationEvent, { signal: notificationController.signal }).catch((caught) => {
    // Older local servers multiplex notifications on the conversation stream.
    // A missing dedicated stream should not hide the support queue itself.
    if (!notificationController?.signal.aborted && !String((caught as Error)?.message || '').includes('404')) {
      conversationError.value = errorMessage(caught)
    }
  })
})
onBeforeUnmount(() => {
  liveController?.abort()
  conversationController?.abort()
  notificationController?.abort()
})
</script>

<template>
  <div class="tickets-layout">
    <section class="ticket-list-panel">
      <div class="panel-heading">
        <div><p class="eyebrow">{{ conversationScope === 'all' ? '全部对话记录' : '客服实时队列' }}</p><strong>{{ visibleConversations.length }} 个{{ conversationScope === 'all' ? '会话' : '待接入会话' }}</strong></div>
        <div class="panel-heading-actions"><button class="icon-button" title="刷新客服队列" aria-label="刷新客服队列" @click="loadSupportConversations"><RefreshCw :size="17" /></button><button class="icon-button" title="维护常见问题知识库" aria-label="维护常见问题知识库" @click="showKnowledge = true"><BookOpen :size="17" /></button></div>
      </div>
      <details v-if="unreadNotifications" class="support-notification-inbox" open>
        <summary><span><MessageCircle :size="14" />管理者通知</span><strong>{{ unreadNotifications }}</strong></summary>
        <div class="support-notification-list">
          <article v-for="(notification, index) in notifications" :key="String(notification.id ?? `${notification.conversation_id}-${notification.created_at}-${index}`)">
            <strong>{{ notification.sender_name || '经营管理者' }}</strong>
            <p>{{ notification.message || notification.content }}</p>
            <small v-if="notification.conversation_id">关联会话 #{{ notification.conversation_id }}</small>
          </article>
        </div>
        <button class="button button--quiet support-notification-clear" type="button" @click="clearNotifications">清除已读通知</button>
      </details>
      <div class="ticket-actions">
        <button class="button button--primary button--wide" @click="showNew = true"><CirclePlus :size="17" />创建工单</button>
        <label class="select-with-icon"><Filter :size="16" /><select v-model="filter" aria-label="工单状态筛选"><option value="pending">待处理</option><option value="open">新建</option><option value="in_progress">处理中</option><option value="all">全部工单</option><option value="resolved">已解决</option></select></label>
      </div>
      <section class="live-conversation-list" aria-label="实时人工会话">
        <div class="live-conversation-heading"><span><MessageCircle :size="15" />{{ conversationScope === 'all' ? '对话记录' : '多用户会话' }}</span><strong>{{ visibleConversations.length }}</strong></div>
        <div class="conversation-scope-control" role="tablist" aria-label="会话记录筛选"><button type="button" :class="{ active: conversationScope === 'pending' }" role="tab" @click="changeConversationScope('pending')">待处理</button><button type="button" :class="{ active: conversationScope === 'all' }" role="tab" @click="changeConversationScope('all')">全部记录</button></div>
        <LoadingState v-if="conversationLoading" compact label="载入会话" />
        <p v-else-if="conversationError" class="inline-error"><AlertCircle :size="14" />{{ conversationError }}</p>
        <template v-else>
          <button v-for="conversation in visibleConversations" :key="conversation.id" class="live-conversation-item" :class="{ 'live-conversation-item--active': detailMode === 'conversation' && selectedConversationId === conversation.id }" type="button" @click="chooseConversation(conversation)">
            <span class="live-conversation-primary"><strong>{{ conversationCustomer(conversation) }}</strong><small>{{ conversation.title || `会话 #${conversation.id}` }}</small><small class="live-conversation-preview">{{ conversationRecentMessage(conversation) }}</small></span>
            <span class="live-conversation-secondary"><StatusBadge :value="conversationStatus(conversation)" /><StatusBadge :value="conversationPriority(conversation)" type="priority" /><small v-if="conversationUnread(conversation)" class="conversation-unread">未读 {{ conversationUnread(conversation) }}</small><small>{{ linkedTicket(conversation) ? `工单 #${linkedTicket(conversation)?.id}` : '未关联工单' }}</small><small>{{ conversationAssignedLabel(conversation) }}</small><time>{{ new Date(conversation.updated_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time></span>
          </button>
        </template>
        <p v-if="!conversationLoading && !conversationError && !visibleConversations.length" class="live-conversation-empty">{{ conversationScope === 'all' ? '暂无历史会话记录' : '暂无等待接入的人工会话' }}</p>
      </section>
      <div class="queue-section-heading"><div><p class="eyebrow">工单队列</p><strong>{{ filter === 'all' ? tickets.length : openCount }} 个{{ filter === 'all' ? '全部工单' : '待处理工单' }}</strong></div><button class="icon-button" title="刷新工单" aria-label="刷新工单" @click="loadTickets"><RefreshCw :size="16" /></button></div>
      <LoadingState v-if="loading" label="正在载入工单" />
      <p v-else-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
      <div v-else class="ticket-list">
        <button v-for="ticket in visibleTickets" :key="ticket.id" class="ticket-list-item" :class="{ 'ticket-list-item--active': selectedId === ticket.id }" @click="choose(ticket)">
          <div><span>{{ ticket.customer_name }}</span><StatusBadge :value="ticket.priority" type="priority" /></div>
          <strong>{{ ticket.question }}</strong>
          <small><StatusBadge :value="ticket.status" />{{ ticket.category }}</small>
        </button>
        <EmptyState v-if="!visibleTickets.length" title="没有匹配工单" description="当前筛选条件下没有需要处理的客服事项。" />
      </div>
    </section>

    <section class="ticket-detail-panel">
      <template v-if="detailMode === 'conversation' && selectedConversationId">
        <div class="support-conversation-stack">
          <SupportAssistantPanel
            :conversation-id="selectedConversationId"
            :customer-name="selectedConversation?.customer_name || selectedConversation?.customer?.name"
          />
          <RealtimeChatPanel
            :conversation-id="selectedConversationId"
            role="support"
            title="客服对话记录"
            :customer-name="selectedConversation?.customer_name || selectedConversation?.customer?.name"
            :assigned-agent-id="selectedConversation?.assigned_agent_id ?? selectedConversation?.assigned_agent?.id"
            :assignment-notice="selectedConversation?.takeover_notice || selectedConversation?.last_notification?.message"
            :control-mode="selectedConversation?.control_mode"
            :ticket-id="selectedConversation?.ticket_id ?? selectedConversation?.related_ticket_id"
            :priority="selectedConversationPriority"
            :status="conversationStatus(selectedConversation)"
            :allow-self-assign="false"
            @updated="updateConversation"
            @closed="closeConversation"
          />
        </div>
      </template>
      <template v-else>
        <EmptyState v-if="!selectedTicket && !loading" title="请选择一张工单" description="从左侧队列中选择工单，即可审核 AI 建议并进行人工确认。" />
        <template v-else-if="selectedTicket">
        <SupportAssistantPanel
          :conversation-id="selectedTicket.conversation_id ?? undefined"
          :customer-name="selectedTicket.customer_name"
        />
        <header class="ticket-detail-header">
          <div><p class="eyebrow">工单 #{{ selectedTicket.id }}</p><h2>{{ selectedTicket.customer_name }}</h2><div class="ticket-meta"><StatusBadge :value="selectedTicket.status" /><span>{{ selectedTicket.category }}</span><span>创建于 {{ new Date(selectedTicket.created_at).toLocaleString('zh-CN') }}</span></div></div>
          <select :value="selectedTicket.status" class="status-select" aria-label="更新工单状态" @change="handleStatusChange"><option value="open">待处理</option><option value="in_progress">处理中</option><option value="resolved">已解决</option></select>
        </header>

        <section class="customer-question"><p class="section-label">客户问题</p><p>{{ selectedTicket.question }}</p></section>

        <section class="suggestion-panel">
          <div class="suggestion-heading"><div><Sparkles :size="18" /><div><p class="section-label">AI 推荐回复模板</p><strong>请审核后再确认发送</strong></div></div><span class="quality-score">规则质检代理 {{ Math.round(selectedTicket.quality_score * 100) }}%</span></div>
          <textarea v-model="reply" class="reply-editor" rows="8" maxlength="4000" aria-label="最终回复内容" />
          <section class="ticket-knowledge-basis" aria-live="polite">
            <p class="section-label">知识库依据</p>
            <LoadingState v-if="basisLoading" compact label="正在检索依据" />
            <p v-else-if="basisError" class="inline-error"><AlertCircle :size="14" />{{ basisError }}</p>
            <div v-else-if="ticketBasis.length" class="ticket-basis-list">
              <article v-for="citation in ticketBasis" :key="citation.document_id" class="citation-item">
                <strong>{{ citation.title }}</strong><span>{{ citation.excerpt }}</span><small>匹配度 {{ Math.round(citation.score * 100) }}%</small>
              </article>
            </div>
            <p v-else class="ticket-basis-empty">当前问题未检索到可展示的知识片段，请人工核验后回复。</p>
          </section>
          <div class="reply-actions">
            <button class="button button--secondary" :disabled="saving" @click="reply = selectedTicket.suggested_reply"><Sparkles :size="16" />恢复建议</button>
            <div><button class="button button--quiet" :disabled="saving" @click="updateTicket('in_progress')"><Save :size="16" />保存草稿</button><button class="button button--primary" :disabled="saving || !reply.trim()" @click="updateTicket('resolved')"><Send :size="16" />确认并解决</button></div>
          </div>
        </section>
        <p v-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
        </template>
      </template>
    </section>

    <div v-if="showNew" class="modal-backdrop" role="presentation" @click.self="showNew = false">
      <form class="modal" @submit.prevent="createTicket">
        <div class="modal-header"><div><p class="eyebrow">新建客服事项</p><h2>创建工单</h2></div><button class="icon-button" type="button" title="关闭" aria-label="关闭" @click="showNew = false"><X :size="19" /></button></div>
        <label class="field-label" for="ticket-customer">客户名称</label><input id="ticket-customer" v-model="newTicket.customer_name" class="field-control" required minlength="2" maxlength="80" />
        <label class="field-label" for="ticket-priority">优先级</label><select id="ticket-priority" v-model="newTicket.priority" class="field-control"><option value="low">低</option><option value="normal">普通</option><option value="high">高</option><option value="urgent">紧急</option></select>
        <label class="field-label" for="ticket-question">客户问题</label><textarea id="ticket-question" v-model="newTicket.question" class="field-control" rows="5" required minlength="5" maxlength="4000" />
        <p v-if="createError" class="form-error">{{ createError }}</p><button class="button button--primary button--wide" :disabled="creating" type="submit"><Check :size="17" />{{ creating ? '正在创建' : '创建工单' }}</button>
      </form>
    </div>

    <div v-if="showKnowledge" class="modal-backdrop" role="presentation" @click.self="showKnowledge = false">
      <section class="knowledge-overlay" role="dialog" aria-modal="true" aria-label="常见问题知识库维护">
        <button class="icon-button knowledge-overlay-close" type="button" title="关闭知识库维护" aria-label="关闭知识库维护" @click="showKnowledge = false"><X :size="19" /></button>
        <KnowledgeView />
      </section>
    </div>
  </div>
</template>
