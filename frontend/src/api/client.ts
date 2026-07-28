import axios, { AxiosError } from 'axios'
import type {
  AdminAuditLogPage,
  AuthResponse,
  AgentTrace,
  ChatMessage,
  ChatResponse,
  Conversation,
  ConversationEvent,
  ConversationMessage,
  AdminConversationDetail,
  AdminConversationSummary,
  DashboardDetail,
  DashboardDetailScope,
  DashboardOverview,
  DashboardReport,
  DifyMediaResponse,
  DifyWorkflowResponse,
  ImageAnalysisResponse,
  KnowledgeDocument,
  KnowledgeReindexResult,
  Setting,
  SupportAgent,
  SupportAssistantResponse,
  SupportNotification,
  Ticket,
  TicketEvent,
  User,
  UserCreatePayload,
  UserPreference,
  UserResetPasswordPayload,
} from '@/types'

export const TOKEN_KEY = 'neusoft-ai-token'
export const AUTH_SESSION_EXPIRED_EVENT = 'neusoft-ai:auth-session-expired'

interface RequestOptions {
  signal?: AbortSignal
}

interface StreamRequestOptions extends RequestOptions {
  timeoutMs?: number
}

function expireAuthSession() {
  const hadSession = Boolean(localStorage.getItem(TOKEN_KEY))
  localStorage.removeItem(TOKEN_KEY)
  if (hadSession) window.dispatchEvent(new Event(AUTH_SESSION_EXPIRED_EVENT))
}

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? '/api/v1',
  timeout: 20_000,
  headers: { 'Content-Type': 'application/json' },
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) expireAuthSession()
    return Promise.reject(error)
  },
)

export function errorMessage(error: unknown): string {
  const response = (error as AxiosError<{ detail?: string | { msg?: string }[] }>).response
  const detail = response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join('；') || '请求参数有误'
  if ((error as Error).message === 'Network Error') return '无法连接服务，请确认后端已经启动。'
  return (error as Error).message || '请求未完成，请稍后重试。'
}

export const authApi = {
  login: (payload: { email: string; password: string }) =>
    api.post<AuthResponse>('/auth/login', payload).then((response) => response.data),
  register: (payload: { email: string; password: string; display_name: string }) =>
    api.post<AuthResponse>('/auth/register', payload).then((response) => response.data),
  me: () => api.get<User>('/auth/me', { timeout: 6_000 }).then((response) => response.data),
}

export const assistantApi = {
  chat: (
    payload: { message: string; conversation_id?: number; mode: 'assistant' | 'knowledge' },
    options: RequestOptions = {},
  ) => api.post<ChatResponse>('/assistant/chat', payload, { signal: options.signal, timeout: 120_000 }).then((response) => response.data),
  conversations: () => api.get<Conversation[]>('/assistant/conversations').then((response) => response.data),
  removeConversation: (conversationId: number) =>
    api.delete<void>(`/assistant/conversations/${conversationId}`).then(() => undefined),
  messages: (conversationId: number) =>
    api.get<ChatMessage[]>(`/assistant/conversations/${conversationId}/messages`).then((response) => response.data),
  feedback: (conversationId: number, payload: { rating: number; helpful: boolean; comment?: string }) =>
    api.post<{ rating: number; helpful: boolean; comment?: string | null; submitted_at: string }>(
      `/assistant/conversations/${conversationId}/feedback`,
      payload,
    ).then((response) => response.data),
  preferences: () => api.get<UserPreference>('/users/me/preferences').then((response) => response.data),
  updatePreferences: (payload: UserPreference) =>
    api.put<UserPreference>('/users/me/preferences', payload).then((response) => response.data),
  analyzeImage: (file: File, prompt: string, options: RequestOptions = {}) => {
    const body = new FormData()
    body.append('file', file)
    body.append('prompt', prompt)
    return api.post<ImageAnalysisResponse>('/assistant/image-analysis', body, { signal: options.signal })
      .then((response) => response.data)
  },
}

export async function streamAssistantChat(
  payload: { message: string; conversation_id?: number; mode: 'assistant' | 'knowledge' },
  handlers: {
    onTrace: (trace: AgentTrace) => void
    onToken: (text: string) => void
    onReset?: (text: string) => void
  },
  options: StreamRequestOptions = {},
): Promise<ChatResponse> {
  const baseUrl = String(api.defaults.baseURL ?? '/api/v1').replace(/\/$/, '')
  const token = localStorage.getItem(TOKEN_KEY)
  const controller = new AbortController()
  const timeoutMs = options.timeoutMs ?? 120_000
  let timedOut = false
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  const forwardAbort = () => controller.abort(options.signal?.reason)
  if (options.signal?.aborted) forwardAbort()
  else options.signal?.addEventListener('abort', forwardAbort, { once: true })
  const timeoutId = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  try {
    const response = await fetch(`${baseUrl}/assistant/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(payload),
      signal: controller.signal,
    })
    if (!response.ok) {
      if (response.status === 401) expireAuthSession()
      let detail = ''
      try {
        const body = await response.json() as { detail?: string }
        detail = body.detail ?? ''
      } catch { /* The fallback request will surface a normal API error. */ }
      const responseError = new Error(detail || `流式回答请求失败（${response.status}）`)
      if (response.status === 401) responseError.name = 'AuthenticationError'
      throw responseError
    }
    if (!response.body) throw new Error('当前环境未返回流式响应。')

    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let finalResponse: ChatResponse | undefined
    let eventName = ''
    let dataLines: string[] = []

    const dispatchEvent = () => {
      const event = eventName || 'message'
      const rawData = dataLines.join('\n')
      eventName = ''
      dataLines = []
      if (!rawData) return
      try {
        if (event === 'trace') handlers.onTrace(JSON.parse(rawData) as AgentTrace)
        if (event === 'token') handlers.onToken((JSON.parse(rawData) as { text?: string }).text ?? '')
        if (event === 'reset') handlers.onReset?.((JSON.parse(rawData) as { text?: string }).text ?? '')
        if (event === 'done') finalResponse = JSON.parse(rawData) as ChatResponse
      } catch {
        // Ignore a malformed event and allow the following SSE event to complete the response.
      }
    }

    const processLine = (line: string) => {
      if (!line) {
        dispatchEvent()
        return
      }
      if (line.startsWith(':')) return

      const separator = line.indexOf(':')
      const field = separator < 0 ? line : line.slice(0, separator)
      let value = separator < 0 ? '' : line.slice(separator + 1)
      if (value.startsWith(' ')) value = value.slice(1)
      if (field === 'event') eventName = value
      if (field === 'data') dataLines.push(value)
    }

    const processBuffer = (atEof: boolean) => {
      let lineStart = 0
      for (let index = 0; index < buffer.length; index += 1) {
        const character = buffer[index]
        if (character !== '\r' && character !== '\n') continue
        if (character === '\r' && index === buffer.length - 1 && !atEof) break

        processLine(buffer.slice(lineStart, index))
        if (character === '\r' && buffer[index + 1] === '\n') index += 1
        lineStart = index + 1
      }
      buffer = buffer.slice(lineStart)
      if (!atEof) return
      if (buffer) processLine(buffer)
      buffer = ''
      dispatchEvent()
    }

    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      processBuffer(done)
      if (done) break
    }
    if (!finalResponse) throw new Error('流式回答未返回完成事件。')
    return finalResponse
  } catch (error) {
    if (timedOut) throw new Error('流式回答等待超时，请稍后重试。')
    throw error
  } finally {
    window.clearTimeout(timeoutId)
    options.signal?.removeEventListener('abort', forwardAbort)
    if (controller.signal.aborted && reader) void reader.cancel().catch(() => undefined)
  }
}

export const knowledgeApi = {
  documents: () => api.get<KnowledgeDocument[]>('/knowledge/documents').then((response) => response.data),
  create: (payload: Pick<KnowledgeDocument, 'title' | 'source' | 'content'>) =>
    api.post<KnowledgeDocument>('/knowledge/documents', payload).then((response) => response.data),
  update: (documentId: number, payload: Pick<KnowledgeDocument, 'title' | 'source' | 'content'>) =>
    api.put<KnowledgeDocument>(`/knowledge/documents/${documentId}`, payload, { timeout: 60_000 })
      .then((response) => response.data),
  remove: (documentId: number) =>
    api.delete<void>(`/knowledge/documents/${documentId}`).then(() => undefined),
  reindex: (documentId: number) =>
    api.post<KnowledgeReindexResult>(`/knowledge/documents/${documentId}/reindex`, undefined, { timeout: 60_000 })
      .then((response) => response.data),
  search: (payload: { query: string; top_k: number }) =>
    api.post<{ results: import('@/types').Citation[] }>('/knowledge/search', payload).then((response) => response.data),
  upload: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return api.post<KnowledgeDocument>('/knowledge/upload', body, {
      headers: { 'Content-Type': 'multipart/form-data' },
    }).then((response) => response.data)
  },
}

export const supportApi = {
  tickets: () => api.get<Ticket[]>('/support/tickets').then((response) => response.data),
  mine: () => api.get<Ticket[]>('/support/tickets/mine').then((response) => response.data),
  get: (ticketId: number) => api.get<Ticket>(`/support/tickets/${ticketId}`).then((response) => response.data),
  create: (payload: { customer_name: string; question: string; priority: string }) =>
    api.post<Ticket>('/support/tickets', payload).then((response) => response.data),
  update: (ticketId: number, payload: { status?: string; final_reply?: string }) =>
    api.patch<Ticket>(`/support/tickets/${ticketId}`, payload).then((response) => response.data),
  assistant: (payload: { query: string; conversation_id?: number; use_knowledge?: boolean }, options: RequestOptions = {}) =>
    api.post<SupportAssistantResponse>('/support/assistant', payload, { signal: options.signal })
      .catch((error: unknown) => {
        // Keep compatibility with the first local backend draft while the
        // canonical endpoint is /support/assistant.
        if (axios.isAxiosError(error) && error.response?.status === 404) {
          return api.post<SupportAssistantResponse>('/support/assistant/chat', payload, { signal: options.signal })
        }
        throw error
      })
      .then((response) => response.data),
  agents: () => api.get<SupportAgent[]>('/executive/support-agents').then((response) => response.data),
  notifications: (unreadOnly = true) =>
    api.get<SupportNotification[]>('/support/notifications', { params: { unread_only: unreadOnly } }).then((response) => response.data),
  markNotificationRead: (notificationId: number) =>
    api.post<SupportNotification>(`/support/notifications/${notificationId}/read`).then((response) => response.data),
}

export async function streamSupportTicketEvents(
  onTicket: (event: TicketEvent) => void,
  options: RequestOptions = {},
): Promise<void> {
  const baseUrl = String(api.defaults.baseURL ?? '/api/v1').replace(/\/$/, '')
  const token = localStorage.getItem(TOKEN_KEY)
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const response = await fetch(`${baseUrl}/support/tickets/events`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: options.signal,
    })
    if (!response.ok) {
      if (response.status === 401) expireAuthSession()
      throw new Error(`实时工单连接失败（${response.status}）`)
    }
    if (!response.body) throw new Error('当前环境未返回实时工单事件流。')

    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, '\n')
      const blocks = buffer.split('\n\n')
      buffer = done ? '' : (blocks.pop() ?? '')
      for (const block of blocks) {
        if (!block.match(/^event:\s*ticket\s*$/m)) continue
        const rawData = block.match(/^data:\s*(.+)$/m)?.[1]
        if (!rawData) continue
        try {
          onTicket(JSON.parse(rawData) as TicketEvent)
        } catch { /* Ignore one malformed event and keep the live connection. */ }
      }
      if (done) break
    }
    if (!options.signal?.aborted) throw new Error('实时工单连接已结束。')
  } finally {
    if (options.signal?.aborted && reader) void reader.cancel().catch(() => undefined)
  }
}

/** User-scoped ticket updates. The server filters events by the authenticated owner. */
export async function streamMyTicketEvents(
  onTicket: (event: TicketEvent) => void,
  options: RequestOptions = {},
): Promise<void> {
  await streamJsonEvents(
    '/support/tickets/mine/events',
    new Set(['ticket']),
    (payload) => onTicket(payload as TicketEvent),
    options,
    '实时工单通知连接失败',
  )
}

async function streamJsonEvents(
  path: string,
  acceptedEvents: Set<string>,
  onData: (payload: unknown) => void,
  options: RequestOptions,
  errorPrefix: string,
): Promise<void> {
  const baseUrl = String(api.defaults.baseURL ?? '/api/v1').replace(/\/$/, '')
  const token = localStorage.getItem(TOKEN_KEY)
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: options.signal,
    })
    if (!response.ok) {
      if (response.status === 401) expireAuthSession()
      throw new Error(`${errorPrefix}（${response.status}）`)
    }
    if (!response.body) throw new Error(`${errorPrefix}：当前环境没有返回事件流`)
    reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let eventName = ''
    let dataLines: string[] = []
    const dispatch = () => {
      const event = eventName || 'message'
      const raw = dataLines.join('\n')
      eventName = ''
      dataLines = []
      if (!raw || !acceptedEvents.has(event)) return
      try { onData(JSON.parse(raw)) } catch { /* Keep the stream alive for one malformed event. */ }
    }
    const consume = (line: string) => {
      if (!line) { dispatch(); return }
      if (line.startsWith(':')) return
      const separator = line.indexOf(':')
      const field = separator < 0 ? line : line.slice(0, separator)
      let value = separator < 0 ? '' : line.slice(separator + 1)
      if (value.startsWith(' ')) value = value.slice(1)
      if (field === 'event') eventName = value
      if (field === 'data') dataLines.push(value)
    }
    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      let lineStart = 0
      for (let index = 0; index < buffer.length; index += 1) {
        if (buffer[index] !== '\n' && buffer[index] !== '\r') continue
        consume(buffer.slice(lineStart, index))
        if (buffer[index] === '\r' && buffer[index + 1] === '\n') index += 1
        lineStart = index + 1
      }
      buffer = buffer.slice(lineStart)
      if (done) {
        if (buffer) consume(buffer)
        dispatch()
        break
      }
    }
    if (!options.signal?.aborted) throw new Error(`${errorPrefix}已结束`)
  } finally {
    if (options.signal?.aborted && reader) void reader.cancel().catch(() => undefined)
  }
}

export const realtimeApi = {
  handoff: (conversationId: number) =>
    api.post<{ conversation_id: number; status: string; message?: ConversationMessage }>(`/assistant/conversations/${conversationId}/handoff`).then((response) => response.data),
  userMessages: (conversationId: number) =>
    api.get<ConversationMessage[]>(`/assistant/conversations/${conversationId}/messages`).then((response) => response.data),
  sendUserMessage: (conversationId: number, content: string) =>
    api.post<ConversationMessage>(`/assistant/conversations/${conversationId}/messages`, { content }).then((response) => response.data),
  supportConversations: (status: 'pending' | 'all' | 'requested' | 'active' | 'closed' = 'pending') =>
    api.get<Conversation[]>('/support/conversations', { params: { status } }).then((response) => response.data),
  supportConversation: (conversationId: number) =>
    api.get<Conversation>(`/support/conversations/${conversationId}`).then((response) => response.data),
  supportMessages: (conversationId: number) =>
    api.get<ConversationMessage[]>(`/support/conversations/${conversationId}/messages`).then((response) => response.data),
  sendSupportMessage: (conversationId: number, content: string) =>
    api.post<ConversationMessage>(`/support/conversations/${conversationId}/messages`, { content }).then((response) => response.data),
  assignConversation: (conversationId: number, assignedAgentId?: number | null) =>
    api.patch<Conversation>(
      `/support/conversations/${conversationId}`,
      assignedAgentId === undefined ? {} : { assigned_agent_id: assignedAgentId },
    )
      .then((response) => response.data),
  closeConversation: (conversationId: number) =>
    api.post<Conversation>(`/support/conversations/${conversationId}/close`).then((response) => response.data),
  markConversationRead: (conversationId: number) =>
    api.post<Conversation>(`/support/conversations/${conversationId}/read`).then((response) => response.data),
}

export function streamUserConversationEvents(
  conversationId: number,
  onEvent: (event: ConversationEvent) => void,
  options: RequestOptions = {},
): Promise<void> {
  return streamJsonEvents(
    `/assistant/conversations/${conversationId}/events`,
    new Set(['conversation']),
    (payload) => onEvent(payload as ConversationEvent),
    options,
    '实时会话连接失败',
  )
}

export function streamSupportConversationEvents(
  onEvent: (event: ConversationEvent) => void,
  options: RequestOptions = {},
): Promise<void> {
  return streamJsonEvents(
    '/support/conversations/events',
    new Set(['conversation', 'notification', 'assignment']),
    (payload) => onEvent(payload as ConversationEvent),
    options,
    '客服会话连接失败',
  )
}

/** Stream manager assignment/notification events for the support inbox. */
export function streamSupportNotificationEvents(
  onEvent: (event: ConversationEvent) => void,
  options: RequestOptions = {},
): Promise<void> {
  return streamJsonEvents(
    '/support/notifications/events',
    new Set(['notification', 'assignment', 'conversation']),
    (payload) => onEvent(payload as ConversationEvent),
    options,
    '客服通知连接失败',
  )
}

export const difyApi = {
  customerService: (payload: { query: string }, options: RequestOptions = {}) =>
    api.post<DifyWorkflowResponse>('/dify/customer-service', payload, { signal: options.signal })
      .then((response) => response.data),
  textToSpeech: (payload: { text: string; voice?: string }, options: RequestOptions = {}) =>
    api.post<DifyMediaResponse>('/dify/text-to-speech', payload, { signal: options.signal, timeout: 60_000 })
      .then((response) => response.data),
  textToImage: (
    payload: { prompt: string; size: '2048*2048' | '2688*1536' | '1536*2688' },
    options: RequestOptions = {},
  ) => api.post<DifyMediaResponse>('/dify/text-to-image', payload, { signal: options.signal, timeout: 130_000 })
    .then((response) => response.data),
  mediaProxy: (
    payload: { url: string; kind: 'audio' | 'image' },
    options: RequestOptions = {},
  ) => api.post<Blob>('/dify/media/proxy', payload, {
    signal: options.signal,
    timeout: 60_000,
    responseType: 'blob',
  }).then((response) => response.data),
}

export const adminApi = {
  users: (params?: { q?: string; role?: string; is_active?: boolean }) =>
    api.get<User[]>('/admin/users', { params }).then((response) => response.data),
  createUser: (payload: UserCreatePayload) =>
    api.post<User>('/admin/users', payload).then((response) => response.data),
  updateUser: (userId: number, payload: { role: User['role']; is_active: boolean }) =>
    api.patch<User>(`/admin/users/${userId}`, payload).then((response) => response.data),
  resetPassword: (userId: number, payload: UserResetPasswordPayload) =>
    api.post<User>(`/admin/users/${userId}/reset-password`, payload).then((response) => response.data),
  deleteUser: (userId: number) =>
    api.delete<User>(`/admin/users/${userId}`).then((response) => response.data),
  settings: () => api.get<Setting[]>('/admin/settings').then((response) => response.data),
  updateSetting: (key: string, payload: { value: string; description: string }) =>
    api.put<Setting>(`/admin/settings/${key}`, payload).then((response) => response.data),
  resetSettings: () => api.put<Setting[]>('/admin/settings-reset').then((response) => response.data),
  auditLogs: (params?: { page?: number; page_size?: number; action?: string }) =>
    api.get<AdminAuditLogPage>('/admin/audit-logs', { params }).then((response) => response.data),
  messages: () => api.get<ChatMessage[]>('/admin/messages').then((response) => response.data),
  conversations: () => api.get<AdminConversationSummary[]>('/admin/conversations').then((response) => response.data),
  conversation: (conversationId: number) =>
    api.get<AdminConversationDetail>(`/admin/conversations/${conversationId}`).then((response) => response.data),
}

export const dashboardApi = {
  overview: () => api.get<DashboardOverview>('/dashboard/overview').then((response) => response.data),
  report: () => api.get<DashboardReport>('/dashboard/report').then((response) => response.data),
  details: (scope: DashboardDetailScope, params: Record<string, string | number> = {}) =>
    api.get<DashboardDetail>('/dashboard/details', { params: { scope, ...params } }).then((response) => response.data),
  conversation: (conversationId: number) =>
    api.get<AdminConversationDetail>(`/dashboard/conversations/${conversationId}`).then((response) => response.data),
}

export const executiveApi = {
  supportAgents: () => api.get<SupportAgent[]>('/executive/support-agents').then((response) => response.data),
  conversation: (conversationId: number) =>
    api.get<Conversation>(`/executive/conversations/${conversationId}`).then((response) => response.data),
  takeoverConversation: (conversationId: number, payload: { assigned_agent_id: number; notice?: string }) =>
    api.post<Conversation>(`/executive/conversations/${conversationId}/takeover`, payload).then((response) => response.data),
  notifyConversation: (conversationId: number, payload: { assigned_agent_id: number; notice: string }) =>
    api.post<SupportNotification>(`/support/conversations/${conversationId}/notify`, payload).then((response) => response.data),
  sendMessage: (conversationId: number, content: string) =>
    api.post<ConversationMessage>(`/executive/conversations/${conversationId}/messages`, { content }).then((response) => response.data),
}
