<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertCircle, CheckCircle2, Headset, ImagePlus, MessageSquarePlus, Mic, RefreshCw, Send, Square, Star, Trash2, Volume2, X } from 'lucide-vue-next'
import { assistantApi, errorMessage, realtimeApi, streamAssistantChat } from '@/api/client'
import ChatMessageBubble from '@/components/ChatMessageBubble.vue'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import RealtimeChatPanel from '@/components/RealtimeChatPanel.vue'
import EnterpriseTicketView from '@/views/EnterpriseTicketView.vue'
import { useSpeech } from '@/composables/useSpeech'
import type { ChatMessage, ChatResponse, Conversation, UserPreference } from '@/types'

const conversations = ref<Conversation[]>([])
const activeConversationId = ref<number | undefined>()
const messages = ref<ChatMessage[]>([])
const message = ref('')
const conversationListLoading = ref(false)
const messageLoading = ref(false)
const conversationLoading = computed(() => conversationListLoading.value || messageLoading.value)
const sending = ref(false)
const conversationError = ref('')
const inputError = ref('')
const messageList = ref<HTMLElement>()
const imageInput = ref<HTMLInputElement>()
const selectedImage = ref<File | null>(null)
const { listening, supported, listen, stop, speak } = useSpeech()
const preferences = ref<UserPreference>({
  response_style: 'balanced',
  preferred_language: 'zh-CN',
  auto_play_voice: false,
})
const preferenceSaving = ref(false)
const handoffAvailable = ref(false)
const handoffConversationId = ref<number>()
const handoffStatus = ref('ai')
const handoffPanelOpen = ref(false)
const handoffLoading = ref(false)
const handoffError = ref('')
const feedbackOpen = ref(false)
const feedbackRating = ref(0)
const feedbackHelpful = ref<boolean | null>(null)
const feedbackComment = ref('')
const feedbackSaving = ref(false)
const feedbackError = ref('')
const feedbackNotice = ref('')
const deletingConversationId = ref<number>()
const showTickets = ref(false)
let conversationListRequest = 0
let messageListRequest = 0
let sendRequest = 0
let localMessageId = 0
let activeSend: { id: number; controller: AbortController } | undefined

const activeConversation = computed(() => conversations.value.find((conversation) => conversation.id === activeConversationId.value))
const hasAiAnswer = computed(() => messages.value.some((item) => (
  (item.role === 'assistant' || item.role === 'ai')
  && !item.client_id?.startsWith('welcome-')
  && !item.pending
  && !item.error
  && item.content.trim()
)))
const conversationRated = computed(() => Boolean(activeConversation.value?.feedback_submitted_at))
const canRateConversation = computed(() => Boolean(activeConversationId.value && hasAiAnswer.value && !conversationRated.value))

function answerOffersHumanSupport(answer: string) {
  return /转人工|人工核验|人工服务|客服人员/.test(answer)
}

function nextLocalMessageId(prefix: string) {
  localMessageId += 1
  return `${prefix}-${localMessageId}`
}

function welcomeMessage(): ChatMessage {
  return {
    client_id: nextLocalMessageId('welcome'),
    role: 'assistant',
    content: '您好，我是商务 AI 助手。您可以咨询合同、发票、订单、账号或服务响应规则；我会结合企业知识库给出有依据的答复。',
    created_at: new Date().toISOString(),
  }
}

async function loadConversations(selectFirst = true) {
  const requestId = ++conversationListRequest
  conversationListLoading.value = true
  conversationError.value = ''
  try {
    const nextConversations = await assistantApi.conversations()
    if (requestId !== conversationListRequest) return
    conversations.value = nextConversations
    if (selectFirst && activeConversationId.value === undefined && nextConversations[0]) {
      await selectConversation(nextConversations[0].id)
    } else if (selectFirst && !nextConversations.length && activeConversationId.value === undefined) {
      newConversation()
    }
  } catch (caught) {
    if (requestId !== conversationListRequest) return
    conversationError.value = errorMessage(caught)
    if (selectFirst && !messages.value.length) messages.value = [welcomeMessage()]
  } finally {
    if (requestId === conversationListRequest) conversationListLoading.value = false
  }
}

async function loadPreferences() {
  try {
    preferences.value = await assistantApi.preferences()
  } catch (caught) {
    inputError.value = errorMessage(caught)
  }
}

async function savePreferences() {
  preferenceSaving.value = true
  inputError.value = ''
  try {
    preferences.value = await assistantApi.updatePreferences({ ...preferences.value })
  } catch (caught) {
    inputError.value = errorMessage(caught)
  } finally {
    preferenceSaving.value = false
  }
}

async function selectConversation(id: number) {
  if (sending.value) return
  if (activeConversationId.value === id && messages.value.length) return
  closeFeedback()
  feedbackNotice.value = ''
  const requestId = ++messageListRequest
  activeConversationId.value = id
  handoffPanelOpen.value = false
  const selectedConversation = conversations.value.find((conversation) => conversation.id === id)
  if (selectedConversation?.handoff_status && !['ai', 'closed'].includes(selectedConversation.handoff_status)) {
    handoffConversationId.value = id
    handoffStatus.value = selectedConversation.handoff_status
    handoffAvailable.value = false
  } else {
    handoffConversationId.value = undefined
    handoffStatus.value = 'ai'
    handoffAvailable.value = false
  }
  messages.value = []
  messageLoading.value = true
  conversationError.value = ''
  try {
    const nextMessages = await assistantApi.messages(id)
    if (requestId !== messageListRequest || activeConversationId.value !== id) return
    messages.value = nextMessages.length ? nextMessages : [welcomeMessage()]
    handoffAvailable.value = !handoffConversationId.value && nextMessages.some((item) => (
      (item.role === 'assistant' || item.role === 'ai') && Boolean(item.content.trim())
    ))
    await scrollToLatest()
  } catch (caught) {
    if (requestId !== messageListRequest || activeConversationId.value !== id) return
    conversationError.value = errorMessage(caught)
  } finally {
    if (requestId === messageListRequest) messageLoading.value = false
  }
}

function newConversation() {
  if (sending.value) return
  closeFeedback()
  feedbackNotice.value = ''
  messageListRequest += 1
  messageLoading.value = false
  activeConversationId.value = undefined
  handoffConversationId.value = undefined
  handoffPanelOpen.value = false
  handoffAvailable.value = false
  handoffStatus.value = 'ai'
  messages.value = [welcomeMessage()]
  conversationError.value = ''
  inputError.value = ''
}

function openFeedback() {
  if (!canRateConversation.value) return
  feedbackRating.value = 0
  feedbackHelpful.value = null
  feedbackComment.value = ''
  feedbackError.value = ''
  feedbackOpen.value = true
}

function closeFeedback() {
  if (feedbackSaving.value) return
  feedbackOpen.value = false
  feedbackError.value = ''
}

async function submitFeedback() {
  const conversationId = activeConversationId.value
  if (!conversationId || feedbackRating.value < 1 || feedbackHelpful.value === null || feedbackSaving.value) {
    feedbackError.value = '请选择评分，并标记本次回复是否有帮助。'
    return
  }
  feedbackSaving.value = true
  feedbackError.value = ''
  try {
    const result = await assistantApi.feedback(conversationId, {
      rating: feedbackRating.value,
      helpful: feedbackHelpful.value,
      ...(feedbackComment.value.trim() ? { comment: feedbackComment.value.trim() } : {}),
    })
    conversations.value = conversations.value.map((conversation) => conversation.id === conversationId
      ? {
          ...conversation,
          feedback_rating: result.rating,
          feedback_helpful: result.helpful,
          feedback_comment: result.comment ?? null,
          feedback_submitted_at: result.submitted_at,
        }
      : conversation)
    feedbackOpen.value = false
    feedbackNotice.value = '感谢你的反馈，已用于 AI 回复满意度统计。'
  } catch (caught) {
    feedbackError.value = errorMessage(caught)
  } finally {
    feedbackSaving.value = false
  }
}

async function deleteConversation(conversation: Conversation) {
  if (sending.value || deletingConversationId.value !== undefined) return
  const confirmed = typeof window === 'undefined' || window.confirm(`确定删除“${conversation.title}”及其全部消息吗？`)
  if (!confirmed) return
  deletingConversationId.value = conversation.id
  conversationError.value = ''
  try {
    await assistantApi.removeConversation(conversation.id)
    const wasActive = activeConversationId.value === conversation.id
    conversations.value = conversations.value.filter((item) => item.id !== conversation.id)
    if (wasActive) {
      activeConversationId.value = undefined
      messages.value = []
      const next = conversations.value[0]
      if (next) await selectConversation(next.id)
      else newConversation()
    }
  } catch (caught) {
    conversationError.value = errorMessage(caught)
  } finally {
    deletingConversationId.value = undefined
  }
}

function chooseImage(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
    inputError.value = '仅支持 PNG、JPEG 和 WebP 图片。'
    input.value = ''
    return
  }
  if (file.size > 5 * 1024 * 1024) {
    inputError.value = '图片不能超过 5MB。'
    input.value = ''
    return
  }
  selectedImage.value = file
  inputError.value = ''
}

function clearImage() {
  selectedImage.value = null
  if (imageInput.value) imageInput.value.value = ''
}

async function scrollToLatest() {
  await nextTick()
  if (messageList.value) messageList.value.scrollTop = messageList.value.scrollHeight
}

function pendingMessage(target: ChatMessage[], clientId: string) {
  return target.find((item) => item.client_id === clientId)
}

function replacePendingMessage(target: ChatMessage[], clientId: string, replacement: ChatMessage) {
  const index = target.findIndex((item) => item.client_id === clientId)
  if (index < 0) return false
  target.splice(index, 1, { ...replacement, client_id: clientId })
  return true
}

function cancelSend() {
  activeSend?.controller.abort()
}

async function send() {
  const content = message.value.trim()
  const image = selectedImage.value
  if (conversationRated.value) {
    inputError.value = '该会话已结束并完成评价，请新建咨询继续提问。'
    return
  }
  if ((!content && !image) || sending.value || conversationLoading.value) return
  const requestId = ++sendRequest
  const controller = new AbortController()
  const requestConversationId = activeConversationId.value
  const requestMessages = messages.value
  const placeholderId = nextLocalMessageId('answer')
  activeSend = { id: requestId, controller }
  inputError.value = ''
  const imagePrompt = content || '请分析图片中与企业服务相关的可见信息，并给出下一步建议。'
  const userMessage: ChatMessage = {
    client_id: nextLocalMessageId('question'),
    role: 'user',
    content: image ? `[图片识别] ${image.name}${content ? `\n${content}` : ''}` : content,
    created_at: new Date().toISOString(),
  }
  const placeholder: ChatMessage = {
    client_id: placeholderId,
    role: 'assistant',
    content: '',
    pending: true,
    created_at: new Date().toISOString(),
  }
  requestMessages.push(userMessage, placeholder)
  message.value = ''
  clearImage()
  sending.value = true
  await scrollToLatest()
  try {
    if (image) {
      const result = await assistantApi.analyzeImage(image, imagePrompt, { signal: controller.signal })
      if (activeSend?.id !== requestId) return
      if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
      replacePendingMessage(requestMessages, placeholderId, {
        role: 'assistant',
        content: result.answer,
        trace: [{ step: '图片理解', status: result.used_fallback ? 'fallback' : 'completed', detail: result.detail }],
        used_fallback: result.used_fallback,
        created_at: new Date().toISOString(),
      })
      handoffAvailable.value = Boolean(result.answer.trim())
      if (preferences.value.auto_play_voice) speak(result.answer, preferences.value.preferred_language)
    } else {
      let receivedAnswerActivity = false
      let result: ChatResponse
      try {
        result = await streamAssistantChat(
          { message: content, conversation_id: requestConversationId, mode: 'assistant' },
          {
            onTrace: (trace) => {
              if (activeSend?.id !== requestId || controller.signal.aborted) return
              const current = pendingMessage(requestMessages, placeholderId)
              if (!current) return
              current.trace = [...(current.trace ?? []), trace]
            },
            onToken: (token) => {
              if (activeSend?.id !== requestId || controller.signal.aborted) return
              const current = pendingMessage(requestMessages, placeholderId)
              if (!current) return
              receivedAnswerActivity = true
              current.pending = false
              current.content += token
              void scrollToLatest()
            },
            onReset: (text) => {
              if (activeSend?.id !== requestId || controller.signal.aborted) return
              const current = pendingMessage(requestMessages, placeholderId)
              if (!current) return
              receivedAnswerActivity = true
              current.pending = false
              current.content = text
              void scrollToLatest()
            },
          },
          { signal: controller.signal },
        )
      } catch (streamError) {
        if (
          controller.signal.aborted
            || receivedAnswerActivity
          || (streamError as Error).name === 'AuthenticationError'
        ) throw streamError
        result = await assistantApi.chat(
          { message: content, conversation_id: requestConversationId, mode: 'assistant' },
          { signal: controller.signal },
        )
      }
      if (activeSend?.id !== requestId) return
      if (controller.signal.aborted) throw new DOMException('请求已取消', 'AbortError')
      activeConversationId.value = result.conversation_id
      handoffAvailable.value = Boolean(result.handoff_available) || Boolean(result.answer.trim()) || answerOffersHumanSupport(result.answer)
      replacePendingMessage(requestMessages, placeholderId, {
        role: 'assistant',
        content: result.answer || pendingMessage(requestMessages, placeholderId)?.content || '',
        citations: result.citations,
        trace: result.trace,
        used_fallback: result.used_fallback,
        artifacts: result.artifacts,
        created_at: new Date().toISOString(),
      })
      if (preferences.value.auto_play_voice) speak(result.answer, preferences.value.preferred_language)
      void loadConversations(false)
    }
  } catch (caught) {
    if (activeSend?.id !== requestId) return
    const partialContent = pendingMessage(requestMessages, placeholderId)?.content.trim()
    replacePendingMessage(requestMessages, placeholderId, controller.signal.aborted
      ? {
          role: 'assistant',
          content: partialContent ? `${partialContent}\n\n（已停止生成）` : '已停止生成。',
          created_at: new Date().toISOString(),
        }
      : {
          role: 'assistant',
          content: errorMessage(caught),
          error: true,
          created_at: new Date().toISOString(),
        })
  } finally {
    if (activeSend?.id === requestId) {
      activeSend = undefined
      sending.value = false
    }
    await scrollToLatest()
  }
}

async function requestHumanSupport() {
  if (handoffConversationId.value) {
    handoffPanelOpen.value = true
    return
  }
  const conversationId = activeConversationId.value
  if (!conversationId || handoffLoading.value) {
    handoffError.value = conversationId ? '' : '请先发送一条消息，再转入人工会话。'
    return
  }
  handoffLoading.value = true
  handoffError.value = ''
  try {
    const result = await realtimeApi.handoff(conversationId)
    handoffConversationId.value = result.conversation_id || conversationId
    handoffStatus.value = result.status
    handoffPanelOpen.value = true
    handoffAvailable.value = false
    conversations.value = conversations.value.map((conversation) => conversation.id === conversationId
      ? { ...conversation, handoff_status: result.status }
      : conversation)
  } catch (caught) {
    handoffError.value = errorMessage(caught)
  } finally {
    handoffLoading.value = false
  }
}

function closeHumanSupport() {
  handoffPanelOpen.value = false
}

function toggleVoice() {
  inputError.value = ''
  if (listening.value) {
    stop()
    return
  }
  listen(
    (text) => { message.value = `${message.value}${message.value ? ' ' : ''}${text}` },
    (error) => { inputError.value = error },
    preferences.value.preferred_language,
  )
}

onMounted(() => { void Promise.all([loadConversations(), loadPreferences()]) })
onBeforeUnmount(() => {
  activeSend?.controller.abort()
  stop()
})
</script>

<template>
  <div class="chat-layout">
    <aside class="conversation-panel">
      <div class="panel-heading">
        <div><p class="eyebrow">会话记录</p><strong>业务咨询</strong></div>
        <button class="icon-button" :disabled="sending" title="刷新会话" aria-label="刷新会话" @click="loadConversations(false)"><RefreshCw :size="17" /></button>
      </div>
      <button class="button button--secondary button--wide" :disabled="sending" @click="newConversation"><MessageSquarePlus :size="17" />新建咨询</button>
      <LoadingState v-if="conversationLoading && !conversations.length" compact label="载入会话中" />
      <p v-else-if="conversationError" class="inline-error"><AlertCircle :size="16" />{{ conversationError }}</p>
      <div v-else class="conversation-list">
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ 'conversation-item--active': activeConversationId === conversation.id }"
        >
          <button class="conversation-item-select" type="button" :disabled="sending || deletingConversationId !== undefined" @click="selectConversation(conversation.id)">
            <strong>{{ conversation.title }}</strong><span>{{ new Date(conversation.updated_at).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) }}</span>
          </button>
          <button class="icon-button icon-button--danger conversation-item-delete" type="button" :disabled="sending || deletingConversationId !== undefined" title="删除这条聊天记录" :aria-label="`删除聊天记录 ${conversation.title}`" @click="deleteConversation(conversation)">
            <Trash2 :size="15" />
          </button>
        </div>
        <EmptyState v-if="!conversations.length" title="暂无历史咨询" description="发起一条业务问题后，会话会自动保存在这里。" />
      </div>
    </aside>

    <section class="chat-workspace">
      <div class="chat-context">
        <div><p class="eyebrow">多 Agent 协同 · 企业知识库增强</p><h2>AI 智能对话助手</h2></div>
        <div class="chat-context-controls">
          <div class="preference-controls">
            <select v-model="preferences.response_style" :disabled="sending || preferenceSaving" aria-label="回答详细程度" @change="savePreferences">
              <option value="concise">简洁</option><option value="balanced">均衡</option><option value="detailed">详细</option>
            </select>
            <select v-model="preferences.preferred_language" :disabled="sending || preferenceSaving" aria-label="回答语言" @change="savePreferences">
              <option value="zh-CN">中文</option><option value="en-US">English</option>
            </select>
            <label><input v-model="preferences.auto_play_voice" :disabled="preferenceSaving" type="checkbox" @change="savePreferences" />自动朗读</label>
          </div>
          <div v-if="handoffAvailable || handoffConversationId" class="handoff-control">
            <button class="button button--quiet" type="button" :disabled="handoffLoading" @click="requestHumanSupport">
              <Headset :size="16" />{{ handoffConversationId ? '查看人工会话' : (handoffLoading ? '正在转接' : '转人工') }}
            </button>
            <span v-if="handoffConversationId" class="handoff-status">{{ handoffStatus === 'active' ? '客服已接入' : '等待客服接入' }}</span>
          </div>
          <button
            v-if="activeConversationId && hasAiAnswer"
            class="button button--quiet conversation-feedback-trigger"
            :disabled="conversationRated || feedbackSaving"
            type="button"
            @click="openFeedback"
          >
            <Star :size="16" :fill="conversationRated ? 'currentColor' : 'none'" />
            {{ conversationRated ? '已完成评价' : '结束并评价' }}
          </button>
          <button class="button button--quiet" type="button" @click="showTickets = true"><MessageSquarePlus :size="16" />我的工单</button>
          <span v-if="feedbackNotice" class="conversation-feedback-notice">{{ feedbackNotice }}</span>
        </div>
      </div>

      <div ref="messageList" class="message-list" aria-live="polite">
        <LoadingState v-if="conversationLoading && !messages.length" label="正在载入对话" />
        <ChatMessageBubble v-for="(item, index) in messages" v-else :key="item.id ?? item.client_id ?? index" :message="item" />
      </div>

      <form class="composer" @submit.prevent="send">
        <div class="composer-topline"><span><CheckCircle2 :size="15" />回答将依据企业知识库生成</span><span v-if="!supported"><Volume2 :size="15" />语音输入在当前浏览器不可用</span></div>
        <div v-if="selectedImage" class="image-attachment"><ImagePlus :size="16" /><span>{{ selectedImage.name }}</span><button class="icon-button" type="button" title="移除图片" aria-label="移除图片" @click="clearImage"><X :size="16" /></button></div>
        <div class="composer-row">
          <textarea v-model="message" :disabled="conversationRated" rows="2" maxlength="4000" placeholder="输入合同、发票、订单、账号或服务问题..." @keydown.enter.exact.prevent="send" />
          <label class="icon-button composer-image" title="上传图片识别" aria-label="上传图片识别"><ImagePlus :size="18" /><input ref="imageInput" type="file" accept="image/png,image/jpeg,image/webp" hidden @change="chooseImage" /></label>
          <button class="icon-button composer-voice" :class="{ 'icon-button--active': listening }" type="button" :title="listening ? '停止语音输入' : '语音输入'" :aria-label="listening ? '停止语音输入' : '语音输入'" @click="toggleVoice">
            <Square v-if="listening" :size="17" /><Mic v-else :size="18" />
          </button>
          <button v-if="sending" class="icon-button icon-button--stop composer-send" type="button" title="停止生成" aria-label="停止生成" @click="cancelSend"><Square :size="16" /></button>
          <button v-else class="icon-button icon-button--primary composer-send" :disabled="conversationRated || conversationLoading || (!message.trim() && !selectedImage)" type="submit" title="发送" aria-label="发送"><Send :size="18" /></button>
        </div>
        <p v-if="inputError" class="inline-error"><AlertCircle :size="15" />{{ inputError }}</p>
      </form>
    </section>

    <div v-if="handoffPanelOpen && handoffConversationId" class="human-chat-overlay" role="dialog" aria-label="实时人工会话">
      <div class="human-chat-overlay__backdrop" @click="closeHumanSupport" />
      <div class="human-chat-overlay__content">
        <button class="icon-button human-chat-overlay__close" type="button" title="关闭人工会话" aria-label="关闭人工会话" @click="closeHumanSupport"><X :size="18" /></button>
        <RealtimeChatPanel :conversation-id="handoffConversationId" role="enterprise" />
      </div>
    </div>
    <p v-if="handoffError" class="inline-error chat-handoff-error" role="alert"><AlertCircle :size="15" />{{ handoffError }}</p>

    <div v-if="feedbackOpen" class="modal-backdrop conversation-feedback-backdrop" role="presentation" @click.self="closeFeedback">
      <section class="modal conversation-feedback-modal" role="dialog" aria-modal="true" aria-labelledby="conversation-feedback-title">
        <div class="modal-header">
          <div><p class="eyebrow">会话结束</p><h2 id="conversation-feedback-title">这次 AI 回复有帮助吗？</h2></div>
          <button class="icon-button" type="button" title="关闭评价" aria-label="关闭评价" :disabled="feedbackSaving" @click="closeFeedback"><X :size="19" /></button>
        </div>
        <p class="conversation-feedback-help">你的评价会用于经营大屏的真实 AI 回复满意度统计。</p>
        <div class="conversation-feedback-stars" aria-label="回复评分">
          <button
            v-for="star in 5"
            :key="star"
            class="conversation-feedback-star"
            :class="{ 'conversation-feedback-star--active': feedbackRating >= star }"
            type="button"
            :title="`${star} 分`"
            :aria-label="`${star} 分`"
            @click="feedbackRating = star"
          ><Star :size="30" :fill="feedbackRating >= star ? 'currentColor' : 'none'" /></button>
        </div>
        <div class="conversation-feedback-choice" role="group" aria-label="是否有帮助">
          <button class="button" :class="{ 'button--primary': feedbackHelpful === true, 'button--secondary': feedbackHelpful !== true }" type="button" @click="feedbackHelpful = true"><CheckCircle2 :size="16" />有帮助</button>
          <button class="button" :class="{ 'button--danger': feedbackHelpful === false, 'button--secondary': feedbackHelpful !== false }" type="button" @click="feedbackHelpful = false"><X :size="16" />没有帮助</button>
        </div>
        <label class="field-label" for="conversation-feedback-comment">补充意见（可选）</label>
        <textarea id="conversation-feedback-comment" v-model="feedbackComment" class="field-control conversation-feedback-comment" rows="3" maxlength="1000" placeholder="告诉我们哪里可以做得更好" :disabled="feedbackSaving" />
        <p v-if="feedbackError" class="form-error" role="alert"><AlertCircle :size="15" />{{ feedbackError }}</p>
        <div class="conversation-feedback-actions">
          <button class="button button--quiet" type="button" :disabled="feedbackSaving" @click="closeFeedback">稍后评价</button>
          <button class="button button--primary" type="button" :disabled="feedbackSaving" @click="submitFeedback">{{ feedbackSaving ? '提交中' : '提交评价' }}</button>
        </div>
      </section>
    </div>

    <div v-if="showTickets" class="human-chat-overlay enterprise-ticket-overlay" role="dialog" aria-label="我的工单">
      <div class="human-chat-overlay__backdrop" @click="showTickets = false" />
      <div class="enterprise-ticket-overlay__content">
        <button class="icon-button human-chat-overlay__close" type="button" title="关闭我的工单" aria-label="关闭我的工单" @click="showTickets = false"><X :size="18" /></button>
        <EnterpriseTicketView />
      </div>
    </div>
  </div>
</template>
