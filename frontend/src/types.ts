export type UserRole = 'enterprise_user' | 'support_agent' | 'admin' | 'executive'

export interface User {
  id: number
  email: string
  display_name: string
  role: UserRole
  is_active: boolean
  created_at: string
  deleted_at?: string | null
}

/** A support-agent row used by manager assignment controls. */
export type SupportAgent = Pick<User, 'id' | 'display_name' | 'email' | 'role' | 'is_active'>

export interface UserPreference {
  response_style: 'concise' | 'balanced' | 'detailed'
  preferred_language: 'zh-CN' | 'en-US'
  auto_play_voice: boolean
}

export interface AuthResponse {
  access_token: string
  token_type: 'bearer'
  user: User
}

export interface Conversation {
  id: number
  title: string
  mode: 'assistant' | 'knowledge' | string
  updated_at: string
  /** Set after the AI conversation is handed to a human agent. */
  handoff_status?: 'ai' | 'requested' | 'active' | 'closed' | string
  assigned_agent_id?: number | null
  /** Optional support-queue fields returned by newer API versions. */
  user_id?: number | null
  customer_id?: number | null
  customer_display_name?: string | null
  user?: { id?: number; display_name?: string; email?: string; role?: string } | null
  customer?: { id?: number; name?: string; display_name?: string; email?: string; role?: string } | null
  customer_name?: string | null
  customer_email?: string | null
  status?: 'ai' | 'requested' | 'active' | 'closed' | string
  unread_count?: number
  priority?: 'low' | 'normal' | 'high' | 'urgent' | string
  ticket_id?: number | null
  related_ticket_id?: number | null
  related_ticket?: Ticket | null
  recent_message?: ConversationMessage | null
  last_message?: ConversationMessage | null
  assigned_agent?: { id?: number; display_name?: string } | null
  takeover_by_id?: number | null
  takeover_by?: { id?: number; display_name?: string; email?: string } | null
  takeover_at?: string | null
  control_mode?: string
  /** Optional manager takeover metadata delivered by the support queue. */
  takeover_notice?: string | null
  last_notification?: SupportNotification | null
  feedback_rating?: number | null
  feedback_helpful?: boolean | null
  feedback_comment?: string | null
  feedback_submitted_at?: string | null
}

export interface ChatMessage {
  id?: number
  client_id?: string
  conversation_id?: number
  role: 'user' | 'assistant' | 'system' | 'support_agent' | 'agent' | string
  content: string
  created_at?: string
  sender_name?: string | null
  sender_role?: string | null
  sender_label?: string | null
  display_role?: string | null
  actor_role?: string | null
  role_label?: string | null
  sender_type?: string | null
  pending?: boolean
  error?: boolean
  citations?: Citation[]
  trace?: AgentTrace[]
  used_fallback?: boolean
  artifacts?: Artifact[]
}

export interface AdminConversationSummary {
  id: number
  title: string
  mode: string
  handoff_status?: string
  status?: string
  updated_at: string
  user_id?: number | null
  customer_name?: string | null
  customer_email?: string | null
  message_count: number
  recent_message?: ChatMessage | null
  assigned_agent_id?: number | null
  assigned_agent?: { id?: number; display_name?: string } | null
  takeover_by_id?: number | null
  takeover_by?: { id?: number; display_name?: string; email?: string } | null
  takeover_at?: string | null
  control_mode?: string
  takeover_notice?: string | null
}

export interface AdminConversationDetail extends AdminConversationSummary {
  messages: ChatMessage[]
}

export interface Citation {
  document_id: number | string
  title: string
  excerpt: string
  score: number
}

export interface AgentTrace {
  step: string
  status: 'completed' | 'skipped' | 'fallback'
  detail: string
}

export interface Artifact {
  kind: 'audio' | 'image'
  media_url?: string | null
  data_url?: string | null
  content_type?: string | null
  byte_size?: number | null
}

export interface ChatResponse {
  conversation_id: number
  answer: string
  citations: Citation[]
  trace: AgentTrace[]
  used_fallback: boolean
  artifacts?: Artifact[]
  handoff_available?: boolean
}

/** Response from the support-only copilot. It is intentionally separate from
 * the enterprise assistant contract so the support workspace can opt out of
 * mandatory RAG while still rendering citations when the model used them. */
export interface SupportAssistantResponse {
  answer: string
  citations: Citation[]
  trace: AgentTrace[]
  used_fallback: boolean
  model_mode?: string | null
  model?: string | null
  knowledge_used?: boolean
}

export interface SupportNotification {
  id?: number | string
  recipient_id?: number
  sender_id?: number
  conversation_id?: number | null
  agent_id?: number | null
  content?: string
  message?: string
  notice?: string | null
  sender_name?: string | null
  kind?: string
  is_read?: boolean
  read_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface ImageAnalysisResponse {
  answer: string
  used_fallback: boolean
  detail: string
}

export interface DifyWorkflowResponse {
  answer: string
  mode: 'remote' | 'local_fallback'
  degraded: boolean
  detail: string
  citations: Citation[]
  trace: AgentTrace[]
}

export interface DifyMediaResponse {
  kind: 'audio' | 'image'
  mode: 'remote'
  degraded: false
  detail: string
  output: Record<string, unknown>
  media_url?: string | null
  data_url?: string | null
  content_type: string
  byte_size?: number | null
}

export interface KnowledgeDocument {
  id: number
  title: string
  source: string
  content: string
  status: string
  created_at: string
  updated_at: string
}

export interface KnowledgeReindexResult {
  document: KnowledgeDocument
  status: string
  indexed_chunks: number
}

export interface Ticket {
  id: number
  requester_id?: number | null
  conversation_id?: number | null
  customer_name: string
  question: string
  category: string
  priority: 'low' | 'normal' | 'high' | 'urgent' | string
  status: 'open' | 'in_progress' | 'resolved' | string
  suggested_reply: string
  final_reply: string | null
  quality_score: number
  created_at: string
  updated_at: string
}

export interface TicketEvent {
  sequence: number
  action: 'created' | 'updated'
  ticket: Ticket
}

export interface ConversationMessage {
  id: number
  conversation_id: number
  role: 'user' | 'assistant' | 'system' | 'support_agent' | 'agent' | 'executive' | 'manager' | string
  content: string
  sender_name?: string | null
  sender_role?: string | null
  sender_label?: string | null
  display_role?: string | null
  actor_role?: string | null
  role_label?: string | null
  sender_type?: string | null
  created_at: string
}

export interface ConversationEvent {
  sequence: number
  action: 'message' | 'handoff' | 'assignment' | 'notification' | string
  conversation_id: number
  message?: ConversationMessage | null
  status?: 'ai' | 'requested' | 'active' | 'closed' | string | null
  assigned_agent_id?: number | null
  assigned_agent?: { id?: number; display_name?: string } | null
  agent_id?: number | null
  notice?: string | null
  takeover_notice?: string | null
  takeover_by_id?: number | null
  control_mode?: string | null
  notification?: SupportNotification | null
}

export interface Setting {
  id: number
  key: string
  value: string
  description: string
  updated_at: string
}

export interface UserCreatePayload {
  email: string
  password: string
  display_name: string
  role: UserRole
  is_active: boolean
}

export interface UserResetPasswordPayload {
  new_password: string
}

export interface AdminAuditLog {
  id: number
  admin_id: number
  admin_name: string
  action: string
  target_type: string
  target_id: number | null
  target_name: string
  detail: string
  success: boolean
  error_message: string
  created_at: string
}

export interface AdminAuditLogPage {
  items: AdminAuditLog[]
  total: number
  page: number
  page_size: number
}

export interface DashboardOverview {
  metrics: DashboardMetric[]
  category_distribution: Array<{ name: string; value: number }>
  satisfaction_trend: Array<{ date: string; value: number }>
  feedback_satisfaction_trend?: Array<{ date: string; value: number }>
  feedback_count?: number
  feedback_helpful_rate?: number | null
  actual_ai_reply_satisfaction?: number | null
  insights: string[]
  ticket_statuses?: {
    total?: number
    pending?: number
    open?: number
    in_progress?: number
    resolved?: number
    urgent?: number
  }
  ticket_summary?: {
    total?: number
    pending?: number
    open?: number
    in_progress?: number
    resolved?: number
    urgent?: number
  }
  ticket_counts?: {
    total?: number
    pending?: number
    open?: number
    in_progress?: number
    resolved?: number
    urgent?: number
  }
  consultation_count?: number
  ai_reply_satisfaction?: number
  satisfaction?: number | string
  urgent_tickets?: number
  system: {
    provider: string
    dify: string
    index: string
  }
}

export interface DashboardReport {
  title: string
  summary: string
}

export type DashboardDetailScope = 'tickets' | 'status' | 'category' | 'consultations' | 'satisfaction' | 'insights'

export interface DashboardDetailRow {
  [key: string]: string | number | boolean | null | undefined
}

export interface DashboardDetail {
  scope: DashboardDetailScope
  title: string
  summary: string
  rows: DashboardDetailRow[]
}

export interface DashboardMetric {
  label: string
  value: string | number
  delta: string
  tone: 'teal' | 'coral' | 'blue' | 'gold'
}
