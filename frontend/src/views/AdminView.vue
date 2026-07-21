<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { AlertCircle, BarChart3, BookOpen, Check, ChevronDown, ClipboardList, Database, FileBarChart, RefreshCw, Save, Settings2, Upload, Users } from 'lucide-vue-next'
import { adminApi, dashboardApi, errorMessage } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import type { AdminConversationDetail, AdminConversationSummary, ChatMessage, DashboardReport, Setting, User, UserRole } from '@/types'

type Tab = 'users' | 'settings' | 'audit' | 'knowledge' | 'report'

const activeTab = ref<Tab>('users')
const users = ref<User[]>([])
const settings = ref<Setting[]>([])
const conversations = ref<AdminConversationSummary[]>([])
const auditDetails = ref<Record<number, AdminConversationDetail>>({})
const auditLoading = ref<Record<number, boolean>>({})
const auditErrors = ref<Record<number, string>>({})
const drafts = ref<Record<string, { value: string; description: string }>>({})
const loading = ref(false)
const error = ref('')
const savingUser = ref<number>()
const savingSetting = ref<string>()
const report = ref<DashboardReport>()
const reportLoading = ref(false)
const reportError = ref('')

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: 'enterprise_user', label: '企业用户' },
  { value: 'support_agent', label: '客服专员' },
  { value: 'executive', label: '经营管理者' },
  { value: 'admin', label: '系统管理员' },
]

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [nextUsers, nextSettings, nextConversations] = await Promise.all([adminApi.users(), adminApi.settings(), adminApi.conversations()])
    users.value = nextUsers
    settings.value = nextSettings
    conversations.value = nextConversations
    drafts.value = Object.fromEntries(nextSettings.map((setting) => [setting.key, { value: setting.value, description: setting.description }]))
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

async function saveUser(user: User) {
  savingUser.value = user.id
  error.value = ''
  try {
    const updated = await adminApi.updateUser(user.id, { role: user.role, is_active: user.is_active })
    users.value.splice(users.value.findIndex((item) => item.id === user.id), 1, updated)
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    savingUser.value = undefined
  }
}

async function saveSetting(setting: Setting) {
  const draft = drafts.value[setting.key]
  if (!draft) return
  savingSetting.value = setting.key
  error.value = ''
  try {
    const updated = await adminApi.updateSetting(setting.key, draft)
    settings.value.splice(settings.value.findIndex((item) => item.key === setting.key), 1, updated)
    drafts.value[setting.key] = { value: updated.value, description: updated.description }
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    savingSetting.value = undefined
  }
}

function hasFallback(message: ChatMessage) {
  return message.trace?.some((trace) => trace.status === 'fallback') ?? false
}

function messageRoleLabel(role: ChatMessage['role']) {
  if (role === 'user' || role === 'enterprise_user') return '企业用户'
  if (role === 'support_agent' || role === 'agent') return '客服'
  if (role === 'executive' || role === 'manager') return '经营管理者'
  if (role === 'system') return '系统'
  return 'AI'
}

function conversationCustomer(conversation: AdminConversationSummary) {
  return conversation.customer_name || `企业用户 #${conversation.user_id ?? conversation.id}`
}

function conversationStatus(conversation: AdminConversationSummary) {
  return conversation.status || conversation.handoff_status || 'ai'
}

async function loadAuditConversation(conversationId: number) {
  if (auditDetails.value[conversationId] || auditLoading.value[conversationId]) return
  auditLoading.value[conversationId] = true
  auditErrors.value[conversationId] = ''
  try {
    auditDetails.value[conversationId] = await adminApi.conversation(conversationId)
  } catch (caught) {
    auditErrors.value[conversationId] = errorMessage(caught)
  } finally {
    auditLoading.value[conversationId] = false
  }
}

function handleAuditToggle(event: Event, conversationId: number) {
  if ((event.target as HTMLDetailsElement).open) void loadAuditConversation(conversationId)
}

async function generateReport() {
  reportLoading.value = true
  reportError.value = ''
  try {
    report.value = await dashboardApi.report()
  } catch (caught) {
    reportError.value = errorMessage(caught)
  } finally {
    reportLoading.value = false
  }
}

function openReport() {
  activeTab.value = 'report'
  void generateReport()
}

onMounted(() => { void loadData() })
</script>

<template>
  <div class="admin-page">
    <section class="page-toolbar">
      <div><p class="eyebrow">平台控制台</p><h2>系统管理</h2><p>维护账户权限、AI 工作参数与关键对话审计记录。</p></div>
      <button class="icon-button" title="刷新管理数据" aria-label="刷新管理数据" @click="loadData"><RefreshCw :size="18" /></button>
    </section>
    <p v-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
    <div class="tabbar" role="tablist" aria-label="系统管理菜单">
      <button :class="{ active: activeTab === 'users' }" role="tab" @click="activeTab = 'users'"><Users :size="17" />用户与权限</button>
      <button :class="{ active: activeTab === 'settings' }" role="tab" @click="activeTab = 'settings'"><Settings2 :size="17" />AI 配置</button>
      <button :class="{ active: activeTab === 'audit' }" role="tab" @click="activeTab = 'audit'"><ClipboardList :size="17" />对话审计</button>
      <button :class="{ active: activeTab === 'knowledge' }" role="tab" @click="activeTab = 'knowledge'"><BookOpen :size="17" />知识库管理</button>
      <button :class="{ active: activeTab === 'report' }" role="tab" @click="openReport"><FileBarChart :size="17" />智能分析报告</button>
    </div>

    <LoadingState v-if="loading" label="正在加载管理数据" />
    <section v-else-if="activeTab === 'users'" class="admin-surface">
      <div class="section-heading"><div><p class="eyebrow">账户目录</p><h3>用户与访问权限</h3></div><span class="count-chip">{{ users.length }} 位用户</span></div>
      <div class="data-table-wrap"><table class="data-table"><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>创建时间</th><th></th></tr></thead><tbody><tr v-for="user in users" :key="user.id"><td><div class="user-cell"><span class="avatar">{{ user.display_name.slice(0, 1) }}</span><div><strong>{{ user.display_name }}</strong><small>{{ user.email }}</small></div></div></td><td><select v-model="user.role" class="table-select" aria-label="用户角色"><option v-for="role in roleOptions" :key="role.value" :value="role.value">{{ role.label }}</option></select></td><td><label class="switch"><input v-model="user.is_active" type="checkbox" /><span /><em>{{ user.is_active ? '已启用' : '已停用' }}</em></label></td><td>{{ new Date(user.created_at).toLocaleDateString('zh-CN') }}</td><td><button class="icon-button" :disabled="savingUser === user.id" title="保存用户设置" aria-label="保存用户设置" @click="saveUser(user)"><Check v-if="savingUser !== user.id" :size="17" /><span v-else class="button-spinner" /></button></td></tr></tbody></table></div>
      <EmptyState v-if="!users.length" title="暂无用户数据" description="后端返回用户记录后，会显示在此处。" />
    </section>

    <section v-else-if="activeTab === 'settings'" class="settings-grid">
      <article v-for="setting in settings" :key="setting.key" class="setting-card">
        <div class="setting-card-heading"><span><Database :size="18" /></span><div><p class="section-label">{{ setting.key }}</p><strong>{{ setting.description || 'AI 运行参数' }}</strong></div></div>
        <select v-if="setting.key === 'default_language'" v-model="drafts[setting.key].value" class="field-control" aria-label="系统默认回答语言">
          <option value="zh-CN">中文</option><option value="en-US">English</option>
        </select>
        <select v-else-if="setting.key === 'reply_strategy'" v-model="drafts[setting.key].value" class="field-control" aria-label="系统默认回复策略">
          <option value="concise">简洁</option><option value="balanced">均衡</option><option value="detailed">详细</option>
        </select>
        <textarea v-else-if="drafts[setting.key]?.value.length > 100" v-model="drafts[setting.key].value" class="field-control setting-value" rows="5" :aria-label="`${setting.key} 的值`" />
        <input v-else v-model="drafts[setting.key].value" class="field-control" :aria-label="`${setting.key} 的值`" />
        <input v-model="drafts[setting.key].description" class="field-control setting-description" placeholder="配置说明" :aria-label="`${setting.key} 的说明`" />
        <button class="button button--secondary" :disabled="savingSetting === setting.key" @click="saveSetting(setting)"><Save :size="16" />{{ savingSetting === setting.key ? '保存中' : '保存配置' }}</button>
      </article>
      <EmptyState v-if="!settings.length" title="暂无运行配置" description="后端返回配置项后，可在这里安全调整。" />
    </section>

    <section v-else-if="activeTab === 'knowledge'" class="admin-embedded-knowledge">
      <div class="admin-knowledge-upload-note"><span><Upload :size="17" /></span><div><strong>知识库文档上传</strong><p>支持 TXT、Markdown、CSV、PDF 和 DOCX；上传后会自动解析并建立检索索引。</p></div></div>
      <KnowledgeView />
    </section>

    <section v-else-if="activeTab === 'report'" class="admin-report-surface">
      <div class="section-heading"><div><p class="eyebrow">智能分析</p><h3>服务运营报告</h3></div><button class="button button--primary" :disabled="reportLoading" @click="generateReport"><BarChart3 :size="16" />{{ reportLoading ? '生成中' : '重新生成报告' }}</button></div>
      <LoadingState v-if="reportLoading" label="正在生成智能分析报告" />
      <p v-else-if="reportError" class="inline-error"><AlertCircle :size="16" />{{ reportError }}</p>
      <template v-else-if="report">
        <h4 class="admin-report-title">{{ report.title }}</h4>
        <p class="admin-report-summary">{{ report.summary }}</p>
      </template>
      <EmptyState v-else title="尚未生成报告" description="点击生成报告，汇总当前咨询、满意度与工单数据。" />
    </section>

    <section v-else class="audit-list">
      <div class="section-heading"><div><p class="eyebrow">全部服务记录</p><h3>按会话查看对话审计</h3></div><span class="count-chip">{{ conversations.length }} 个会话</span></div>
      <div v-if="conversations.length" class="audit-conversation-list">
        <details v-for="(conversation, index) in conversations" :key="conversation.id" class="audit-conversation" @toggle="handleAuditToggle($event, conversation.id)">
          <summary class="audit-conversation-summary">
            <span class="audit-index">{{ String(index + 1).padStart(2, '0') }}</span>
            <span class="audit-conversation-main"><strong>{{ conversationCustomer(conversation) }}</strong><small>{{ conversation.title || `会话 #${conversation.id}` }} · {{ conversation.message_count }} 条消息</small></span>
            <span class="audit-conversation-meta"><StatusBadge :value="conversationStatus(conversation)" /><time>{{ new Date(conversation.updated_at).toLocaleString('zh-CN') }}</time><ChevronDown class="audit-conversation-chevron" :size="16" /></span>
          </summary>
          <div class="audit-conversation-body">
            <LoadingState v-if="auditLoading[conversation.id]" compact label="正在载入完整会话" />
            <p v-else-if="auditErrors[conversation.id]" class="inline-error"><AlertCircle :size="14" />{{ auditErrors[conversation.id] }}</p>
            <template v-else-if="auditDetails[conversation.id]">
              <article v-for="message in auditDetails[conversation.id].messages" :key="message.id" class="audit-item">
                <span class="audit-index">{{ messageRoleLabel(message.role) }}</span>
                <div class="audit-item-content"><strong>{{ message.content }}</strong><p>{{ message.sender_label || messageRoleLabel(message.role) }}</p>
                  <details v-if="message.citations?.length || message.trace?.length" class="audit-details">
                    <summary><ChevronDown class="audit-details-chevron" :size="15" />审计详情<span>{{ message.citations?.length ?? 0 }} 条依据 · {{ message.trace?.length ?? 0 }} 步轨迹</span></summary>
                    <div class="audit-details-body">
                      <section v-if="message.citations?.length" class="audit-detail-group"><h4>知识依据</h4><div v-for="(citation, citationIndex) in message.citations" :key="`${citation.document_id}-${citationIndex}`" class="audit-citation-row"><div><strong>{{ citation.title }}</strong><small>文档 #{{ citation.document_id }} · 匹配度 {{ Math.round(citation.score * 100) }}%</small></div><span>{{ citation.excerpt }}</span></div></section>
                      <section v-if="message.trace?.length" class="audit-detail-group"><h4>处理轨迹</h4><div v-for="(trace, traceIndex) in message.trace" :key="`${trace.step}-${traceIndex}`" class="audit-trace-row"><strong>{{ trace.step }}</strong><StatusBadge :value="trace.status" type="trace" /><span>{{ trace.detail }}</span></div></section>
                    </div>
                  </details>
                </div>
                <time>{{ message.created_at ? new Date(message.created_at).toLocaleString('zh-CN') : '时间未知' }}</time>
                <StatusBadge v-if="hasFallback(message)" value="fallback" type="trace" />
              </article>
              <EmptyState v-if="!auditDetails[conversation.id].messages.length" title="该会话暂无消息" description="会话记录为空。" />
            </template>
          </div>
        </details>
      </div>
      <EmptyState v-else title="暂无可审计对话" description="后端保留的全部会话记录将显示在这里。" />
    </section>
  </div>
</template>
