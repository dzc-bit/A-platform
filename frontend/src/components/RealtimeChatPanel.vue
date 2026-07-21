<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { MessageCircle, RefreshCw, Send, Wifi, WifiOff } from 'lucide-vue-next'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import { errorMessage, realtimeApi, streamSupportConversationEvents, streamUserConversationEvents } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { ConversationEvent, ConversationMessage } from '@/types'

const props = withDefaults(defineProps<{
  conversationId?: number
  role: 'enterprise' | 'support'
  title?: string
  customerName?: string | null
  assignedAgentId?: number | null
  ticketId?: number | null
  priority?: string | null
  status?: string | null
  /** Support agents receive manager assignment notifications; self-claiming
   * is opt-in for legacy/admin embeds and disabled by the support workspace. */
  allowSelfAssign?: boolean
  assignmentNotice?: string | null
  controlMode?: string | null
}>(), {
  title: '实时人工会话',
  customerName: null,
  assignedAgentId: null,
  ticketId: null,
  priority: null,
  status: null,
  allowSelfAssign: false,
  assignmentNotice: null,
  controlMode: 'support_agent',
})
const auth = useAuthStore()

const emit = defineEmits<{
  closed: [conversationId: number]
  updated: [conversation: import('@/types').Conversation]
}>()

const messages = ref<ConversationMessage[]>([])
const loading = ref(false)
const sending = ref(false)
const refreshing = ref(false)
const content = ref('')
const error = ref('')
const connected = ref(false)
const handoffStatus = ref(props.status || 'requested')
const assignedAgentId = ref<number | null>(props.assignedAgentId ?? null)
const assignmentNotice = ref(props.assignmentNotice ?? '')
const assigning = ref(false)
const closing = ref(false)
const messageList = ref<HTMLElement>()
let controller: AbortController | undefined
let reconnectTimer: ReturnType<typeof window.setTimeout> | undefined
let reconnectAttempt = 0

const hasConversation = computed(() => Number.isInteger(props.conversationId) && (props.conversationId ?? 0) > 0)
const connectionLabel = computed(() => connected.value ? '实时连接' : '等待连接')
const statusLabel = computed(() => ({
  ai: 'AI 处理中',
  requested: '等待客服接入',
  active: '客服已接入',
  closed: '会话已结束',
}[handoffStatus.value] ?? handoffStatus.value))
const canReply = computed(() => props.role !== 'support' || props.controlMode !== 'executive_takeover')
const roleLabel = (role: ConversationMessage['role']) => {
  if (role === 'user') return '企业用户'
  if (role === 'support_agent' || role === 'agent') return '客服'
  if (role === 'executive' || role === 'manager') return '经营管理者'
  if (role === 'system') return '系统'
  return 'AI'
}
const roleAvatar = (role: ConversationMessage['role']) => {
  if (role === 'user') return '企'
  if (role === 'support_agent' || role === 'agent') return '客'
  if (role === 'executive' || role === 'manager') return '管'
  if (role === 'system') return '系'
  return 'AI'
}

async function scrollLatest() {
  await nextTick()
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

function appendMessage(message: ConversationMessage) {
  if (!message || messages.value.some((item) => item.id === message.id)) return
  messages.value.push(message)
  messages.value.sort((left, right) => left.created_at.localeCompare(right.created_at) || left.id - right.id)
}

function receiveEvent(event: ConversationEvent) {
  if (event.conversation_id !== props.conversationId) return
  if (event.status) handoffStatus.value = event.status
  if (event.assigned_agent_id !== undefined) assignedAgentId.value = event.assigned_agent_id
  if (event.notice !== undefined && event.notice !== null) assignmentNotice.value = event.notice
  if (event.takeover_notice !== undefined && event.takeover_notice !== null) assignmentNotice.value = event.takeover_notice
  if (event.notification?.message || event.notification?.content) assignmentNotice.value = event.notification.message || event.notification.content || ''
  if (event.message) {
    appendMessage(event.message)
    void scrollLatest()
  }
}

function stopStream() {
  controller?.abort()
  controller = undefined
  connected.value = false
  if (reconnectTimer !== undefined) {
    window.clearTimeout(reconnectTimer)
    reconnectTimer = undefined
  }
}

function scheduleReconnect() {
  if (!hasConversation.value || reconnectTimer !== undefined) return
  const delay = Math.min(15_000, 800 * (2 ** Math.min(reconnectAttempt, 4)))
  reconnectAttempt += 1
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = undefined
    void connect()
  }, delay)
}

async function connect() {
  stopStream()
  if (!hasConversation.value) return
  const id = props.conversationId as number
  controller = new AbortController()
  const activeController = controller
  connected.value = false
  try {
    const stream = props.role === 'support'
      ? streamSupportConversationEvents(receiveEvent, { signal: activeController.signal })
      : streamUserConversationEvents(id, receiveEvent, { signal: activeController.signal })
    connected.value = true
    reconnectAttempt = 0
    await stream
  } catch (caught) {
    if (!activeController.signal.aborted) {
      connected.value = false
      // A missing handoff endpoint should leave the panel usable while the
      // rest of the AI workspace remains available.
      error.value = errorMessage(caught)
      scheduleReconnect()
    }
  }
}

async function loadMessages() {
  stopStream()
  messages.value = []
  error.value = ''
  if (!hasConversation.value) return
  loading.value = true
  try {
    const id = props.conversationId as number
    const result = props.role === 'support'
      ? await realtimeApi.supportMessages(id)
      : await realtimeApi.userMessages(id)
    messages.value = result
    void scrollLatest()
    reconnectAttempt = 0
    void connect()
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

async function refresh() {
  refreshing.value = true
  try { await loadMessages() } finally { refreshing.value = false }
}

async function send() {
  const text = content.value.trim()
  if (!text || !hasConversation.value || sending.value) return
  sending.value = true
  error.value = ''
  try {
    const id = props.conversationId as number
    const result = props.role === 'support'
      ? await realtimeApi.sendSupportMessage(id, text)
      : await realtimeApi.sendUserMessage(id, text)
    appendMessage(result)
    content.value = ''
    await scrollLatest()
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    sending.value = false
  }
}

async function assignConversation() {
  if (!hasConversation.value || assigning.value || handoffStatus.value === 'closed') return
  assigning.value = true
  error.value = ''
  try {
    const result = await realtimeApi.assignConversation(props.conversationId as number, auth.user?.id)
    assignedAgentId.value = result.assigned_agent_id ?? null
    handoffStatus.value = result.handoff_status ?? 'active'
    emit('updated', result)
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    assigning.value = false
  }
}

async function closeConversation() {
  if (!hasConversation.value || closing.value || handoffStatus.value === 'closed') return
  if (typeof window !== 'undefined' && !window.confirm('结束当前人工会话吗？')) return
  closing.value = true
  error.value = ''
  try {
    const result = await realtimeApi.closeConversation(props.conversationId as number)
    assignedAgentId.value = result.assigned_agent_id ?? assignedAgentId.value
    handoffStatus.value = result.handoff_status ?? 'closed'
    emit('updated', result)
    emit('closed', props.conversationId as number)
    stopStream()
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    closing.value = false
  }
}

watch(() => [props.conversationId, props.role, props.assignedAgentId, props.status, props.assignmentNotice, props.controlMode] as const, () => {
  assignedAgentId.value = props.assignedAgentId ?? null
  assignmentNotice.value = props.assignmentNotice ?? ''
  handoffStatus.value = props.status || 'requested'
  void loadMessages()
})
onMounted(() => { void loadMessages() })
onBeforeUnmount(stopStream)
</script>

<template>
  <section class="realtime-chat-panel" aria-live="polite">
    <header class="realtime-chat-header">
      <div class="realtime-chat-title">
        <span class="realtime-chat-icon"><MessageCircle :size="18" /></span>
        <div><p class="eyebrow">{{ title }}</p><strong>{{ role === 'support' ? '人工客服工作台' : '客服跟进' }}</strong></div>
      </div>
      <div class="realtime-chat-status" :class="{ 'realtime-chat-status--offline': !connected }">
        <Wifi v-if="connected" :size="14" /><WifiOff v-else :size="14" />
        <span>{{ connectionLabel }}</span>
      </div>
    </header>

    <div v-if="hasConversation" class="realtime-chat-meta">
      <span>{{ customerName || '企业用户' }}</span><span>{{ statusLabel }}</span>
      <span v-if="priority">优先级 {{ priority }}</span><span v-if="ticketId">工单 #{{ ticketId }}</span>
      <button class="icon-button" type="button" title="刷新会话" aria-label="刷新会话" :disabled="refreshing || loading" @click="refresh"><RefreshCw :size="15" :class="{ 'is-spinning': refreshing }" /></button>
    </div>
    <div v-if="hasConversation && role === 'support'" class="realtime-chat-actions">
      <span class="realtime-assignment">{{ assignedAgentId ? `已分配客服 #${assignedAgentId}` : '尚未分配客服' }}</span>
      <button v-if="allowSelfAssign" class="button button--quiet" type="button" :disabled="assigning || closing || handoffStatus === 'closed' || Boolean(assignedAgentId)" @click="assignConversation"><MessageCircle :size="15" />{{ assigning ? '分配中' : '接管会话' }}</button>
      <button v-if="canReply" class="button button--danger" type="button" :disabled="assigning || closing || handoffStatus === 'closed'" @click="closeConversation"><WifiOff :size="15" />{{ closing ? '结束中' : '结束会话' }}</button>
    </div>
    <p v-if="hasConversation && role === 'support' && assignmentNotice" class="realtime-assignment-notice">管理者通知：{{ assignmentNotice }}</p>

    <div v-if="!hasConversation" class="realtime-chat-empty">
      <EmptyState title="尚未进入人工会话" description="AI 客服需要人工跟进时，会在这里建立实时会话。" />
    </div>
    <div v-else ref="messageList" class="realtime-message-list">
      <LoadingState v-if="loading" compact label="正在加载会话" />
      <EmptyState v-else-if="!messages.length" title="等待第一条消息" description="发送消息后，客服会在此会话中回复。" />
      <template v-else>
        <article v-for="message in messages" :key="message.id" class="realtime-message" :class="`realtime-message--${message.role}`">
          <div class="realtime-message-meta"><span class="realtime-message-avatar">{{ roleAvatar(message.role) }}</span><strong>{{ roleLabel(message.role) }}</strong><time>{{ new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time></div>
          <p>{{ message.content }}</p>
        </article>
      </template>
    </div>

    <form v-if="hasConversation && handoffStatus !== 'closed' && canReply" class="realtime-chat-composer" @submit.prevent="send">
      <textarea v-model="content" rows="2" maxlength="4000" :placeholder="role === 'support' ? '输入给企业用户的回复…' : '补充问题，客服会实时看到…'" :disabled="sending" @keydown.enter.exact.prevent="send" />
      <button class="button button--primary" type="submit" :disabled="sending || !content.trim()"><Send :size="16" />{{ sending ? '发送中' : '发送' }}</button>
    </form>
    <p v-else-if="hasConversation && handoffStatus !== 'closed' && role === 'support'" class="realtime-readonly-notice">经营管理者已接管当前会话，客服仅接收通知。</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
  </section>
</template>
