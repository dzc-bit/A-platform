<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertCircle, BarChart3, BookOpen, Check, ChevronDown, ClipboardList, Database, FileBarChart, KeyRound, Plus, RefreshCw, RotateCcw, Save, ScrollText, Search, Settings2, Trash2, Upload, UserPlus, Users, X } from 'lucide-vue-next'
import { adminApi, dashboardApi, errorMessage } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import KnowledgeView from '@/views/KnowledgeView.vue'
import type { AdminAuditLog, AdminAuditLogPage, AdminConversationDetail, AdminConversationSummary, ChatMessage, DashboardReport, Setting, User, UserRole } from '@/types'

type Tab = 'users' | 'settings' | 'audit' | 'knowledge' | 'report' | 'adminlog'

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
const report = ref<DashboardReport>()
const reportLoading = ref(false)
const reportError = ref('')

// --- Toast system ---
interface Toast { id: number; type: 'success' | 'error'; message: string }
const toasts = ref<Toast[]>([])
let toastId = 0
function showToast(type: 'success' | 'error', message: string) {
  const id = ++toastId
  toasts.value.push({ id, type, message })
  setTimeout(() => { toasts.value = toasts.value.filter((t) => t.id !== id) }, 2500)
}

// --- User management state ---
const userSearch = ref('')
const userRoleFilter = ref('')
const userStatusFilter = ref('')
const savingUser = ref<number>()
const userSaveSuccess = ref<number>()
const userSnapshots = ref<Record<number, { role: UserRole; is_active: boolean }>>({})

// Create user modal
const showCreateModal = ref(false)
const createForm = ref({ display_name: '', email: '', password: '', role: 'enterprise_user' as UserRole, is_active: true })
const createSaving = ref(false)
const createError = ref('')

// Reset password modal
const showResetModal = ref(false)
const resetTarget = ref<User>()
const resetPassword = ref('')
const resetSaving = ref(false)
const resetError = ref('')

// Confirm dialog
const confirmDialog = ref<{ title: string; message: string; onConfirm: () => void } | null>(null)

// Settings state
const savingSetting = ref<string>()
const settingSaveSuccess = ref<string>()
const settingSnapshots = ref<Record<string, { value: string; description: string }>>({})
const resettingSettings = ref(false)

// Admin audit log state
const adminLogs = ref<AdminAuditLog[]>([])
const adminLogsTotal = ref(0)
const adminLogsPage = ref(1)
const adminLogsLoading = ref(false)
const adminLogsError = ref('')
const adminLogsActionFilter = ref('')
const ADMIN_LOGS_PAGE_SIZE = 15

const roleOptions: Array<{ value: UserRole; label: string }> = [
  { value: 'enterprise_user', label: '企业用户' },
  { value: 'support_agent', label: '客服专员' },
  { value: 'executive', label: '经营管理者' },
  { value: 'admin', label: '系统管理员' },
]

const actionLabels: Record<string, string> = {
  create_user: '新增用户',
  update_user: '修改用户',
  delete_user: '删除用户',
  reset_password: '重置密码',
  update_setting: '修改配置',
  reset_settings: '恢复默认配置',
  upload_document: '上传文档',
  delete_document: '删除文档',
  reindex_document: '重建索引',
}

const filteredUsers = computed(() => {
  let result = users.value
  if (userSearch.value.trim()) {
    const q = userSearch.value.trim().toLowerCase()
    result = result.filter((u) => u.display_name.toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
  }
  if (userRoleFilter.value) result = result.filter((u) => u.role === userRoleFilter.value)
  if (userStatusFilter.value) result = result.filter((u) => userStatusFilter.value === 'active' ? u.is_active : !u.is_active)
  return result
})

const adminLogsTotalPages = computed(() => Math.max(1, Math.ceil(adminLogsTotal.value / ADMIN_LOGS_PAGE_SIZE)))

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [nextUsers, nextSettings, nextConversations] = await Promise.all([adminApi.users(), adminApi.settings(), adminApi.conversations()])
    users.value = nextUsers
    settings.value = nextSettings
    conversations.value = nextConversations
    drafts.value = Object.fromEntries(nextSettings.map((s) => [s.key, { value: s.value, description: s.description }]))
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

// --- User CRUD ---
function snapshotUser(user: User) {
  userSnapshots.value[user.id] = { role: user.role, is_active: user.is_active }
}

async function saveUser(user: User) {
  if (savingUser.value === user.id) return
  snapshotUser(user)
  savingUser.value = user.id
  try {
    const updated = await adminApi.updateUser(user.id, { role: user.role, is_active: user.is_active })
    const idx = users.value.findIndex((item) => item.id === user.id)
    if (idx >= 0) users.value.splice(idx, 1, updated)
    userSaveSuccess.value = user.id
    showToast('success', `用户 ${updated.display_name} 已保存`)
    setTimeout(() => { if (userSaveSuccess.value === user.id) userSaveSuccess.value = undefined }, 1800)
  } catch (caught) {
    const snap = userSnapshots.value[user.id]
    if (snap) { user.role = snap.role; user.is_active = snap.is_active }
    showToast('error', errorMessage(caught))
  } finally {
    savingUser.value = undefined
  }
}

function openCreateModal() {
  createForm.value = { display_name: '', email: '', password: '', role: 'enterprise_user', is_active: true }
  createError.value = ''
  showCreateModal.value = true
}

async function submitCreateUser() {
  if (!createForm.value.display_name.trim() || !createForm.value.email.trim() || createForm.value.password.length < 8) {
    createError.value = '请填写完整信息（密码至少 8 位）'
    return
  }
  createSaving.value = true
  createError.value = ''
  try {
    const newUser = await adminApi.createUser(createForm.value)
    users.value.unshift(newUser)
    showCreateModal.value = false
    showToast('success', `用户 ${newUser.display_name} 创建成功`)
  } catch (caught) {
    createError.value = errorMessage(caught)
  } finally {
    createSaving.value = false
  }
}

function openResetModal(user: User) {
  resetTarget.value = user
  resetPassword.value = ''
  resetError.value = ''
  showResetModal.value = true
}

async function submitResetPassword() {
  if (!resetTarget.value) return
  if (resetPassword.value.length < 8) { resetError.value = '新密码至少 8 位'; return }
  resetSaving.value = true
  resetError.value = ''
  try {
    await adminApi.resetPassword(resetTarget.value.id, { new_password: resetPassword.value })
    showResetModal.value = false
    showToast('success', `已重置 ${resetTarget.value.display_name} 的密码`)
  } catch (caught) {
    resetError.value = errorMessage(caught)
  } finally {
    resetSaving.value = false
  }
}

function confirmDeactivate(user: User) {
  if (!user.is_active) {
    // Re-activating, no confirm needed
    user.is_active = true
    void saveUser(user)
    return
  }
  confirmDialog.value = {
    title: '确认停用用户',
    message: `确定要停用用户「${user.display_name}」吗？停用后该用户将无法登录系统。`,
    onConfirm: () => { user.is_active = false; void saveUser(user) },
  }
}

function executeConfirm() {
  confirmDialog.value?.onConfirm()
  confirmDialog.value = null
}

function confirmDeleteUser(user: User) {
  confirmDialog.value = {
    title: '确认删除用户',
    message: `确定要删除用户「${user.display_name}」吗？删除后该用户将无法登录，但其历史对话和工单数据会保留。`,
    onConfirm: () => void doDeleteUser(user),
  }
}

async function doDeleteUser(user: User) {
  try {
    await adminApi.deleteUser(user.id)
    users.value = users.value.filter((u) => u.id !== user.id)
    showToast('success', `用户 ${user.display_name} 已删除`)
  } catch (caught) {
    showToast('error', errorMessage(caught))
  }
}

// --- Settings ---
function snapshotSetting(key: string) {
  const draft = drafts.value[key]
  if (draft) settingSnapshots.value[key] = { ...draft }
}

async function saveSetting(setting: Setting) {
  const draft = drafts.value[setting.key]
  if (!draft || savingSetting.value === setting.key) return
  snapshotSetting(setting.key)
  savingSetting.value = setting.key
  try {
    const updated = await adminApi.updateSetting(setting.key, draft)
    const idx = settings.value.findIndex((item) => item.key === setting.key)
    if (idx >= 0) settings.value.splice(idx, 1, updated)
    drafts.value[setting.key] = { value: updated.value, description: updated.description }
    settingSaveSuccess.value = setting.key
    showToast('success', `配置 ${setting.key} 已保存`)
    setTimeout(() => { if (settingSaveSuccess.value === setting.key) settingSaveSuccess.value = undefined }, 1800)
  } catch (caught) {
    const snap = settingSnapshots.value[setting.key]
    if (snap) drafts.value[setting.key] = { ...snap }
    showToast('error', errorMessage(caught))
  } finally {
    savingSetting.value = undefined
  }
}

function confirmResetSettings() {
  confirmDialog.value = {
    title: '恢复默认配置',
    message: '确定要将所有 AI 配置恢复为系统默认值吗？此操作不可撤销。',
    onConfirm: () => void doResetSettings(),
  }
}

async function doResetSettings() {
  resettingSettings.value = true
  try {
    const updated = await adminApi.resetSettings()
    settings.value = updated
    drafts.value = Object.fromEntries(updated.map((s) => [s.key, { value: s.value, description: s.description }]))
    showToast('success', '所有配置已恢复为默认值')
  } catch (caught) {
    showToast('error', errorMessage(caught))
  } finally {
    resettingSettings.value = false
  }
}

// --- Admin audit logs ---
async function loadAdminLogs() {
  adminLogsLoading.value = true
  adminLogsError.value = ''
  try {
    const result: AdminAuditLogPage = await adminApi.auditLogs({
      page: adminLogsPage.value,
      page_size: ADMIN_LOGS_PAGE_SIZE,
      action: adminLogsActionFilter.value || undefined,
    })
    adminLogs.value = result.items
    adminLogsTotal.value = result.total
  } catch (caught) {
    adminLogsError.value = errorMessage(caught)
  } finally {
    adminLogsLoading.value = false
  }
}

function switchToAdminLog() {
  activeTab.value = 'adminlog'
  void loadAdminLogs()
}

// --- Conversation audit ---
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

// --- Report ---
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
    <!-- Toast notifications -->
    <div class="admin-toasts" aria-live="polite" aria-atomic="false">
      <TransitionGroup name="toast">
        <div v-for="toast in toasts" :key="toast.id" :class="['admin-toast', `admin-toast--${toast.type}`]" role="status">
          <Check v-if="toast.type === 'success'" :size="15" />
          <AlertCircle v-else :size="15" />
          {{ toast.message }}
        </div>
      </TransitionGroup>
    </div>

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
      <button :class="{ active: activeTab === 'adminlog' }" role="tab" @click="switchToAdminLog"><ScrollText :size="17" />操作日志</button>
      <button :class="{ active: activeTab === 'report' }" role="tab" @click="openReport"><FileBarChart :size="17" />智能分析报告</button>
    </div>

    <LoadingState v-if="loading" label="正在加载管理数据" />

    <!-- ==================== USERS TAB ==================== -->
    <section v-else-if="activeTab === 'users'" class="admin-surface">
      <div class="section-heading">
        <div><p class="eyebrow">账户目录</p><h3>用户与访问权限</h3></div>
        <div class="section-heading-actions">
          <span class="count-chip">{{ filteredUsers.length }} 位用户</span>
          <button class="button button--primary" @click="openCreateModal"><UserPlus :size="16" />新增用户</button>
        </div>
      </div>

      <!-- Search & Filter bar -->
      <div class="admin-filter-bar">
        <div class="filter-search">
          <Search :size="15" />
          <input v-model="userSearch" type="search" placeholder="搜索姓名或邮箱…" class="field-control" aria-label="搜索用户" />
        </div>
        <select v-model="userRoleFilter" class="field-control filter-select" aria-label="按角色筛选">
          <option value="">全部角色</option>
          <option v-for="role in roleOptions" :key="role.value" :value="role.value">{{ role.label }}</option>
        </select>
        <select v-model="userStatusFilter" class="field-control filter-select" aria-label="按状态筛选">
          <option value="">全部状态</option>
          <option value="active">已启用</option>
          <option value="inactive">已停用</option>
        </select>
      </div>

      <div class="data-table-wrap">
        <table class="data-table">
          <thead><tr><th>用户</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="user in filteredUsers" :key="user.id" :class="{ 'row-success': userSaveSuccess === user.id }">
              <td>
                <div class="user-cell">
                  <span class="avatar">{{ user.display_name.slice(0, 1) }}</span>
                  <div><strong>{{ user.display_name }}</strong><small>{{ user.email }}</small></div>
                </div>
              </td>
              <td>
                <select v-model="user.role" class="table-select" aria-label="用户角色" @focus="snapshotUser(user)">
                  <option v-for="role in roleOptions" :key="role.value" :value="role.value">{{ role.label }}</option>
                </select>
              </td>
              <td>
                <label class="switch">
                  <input type="checkbox" :checked="user.is_active" aria-label="启用状态" @change="confirmDeactivate(user)" />
                  <span /><em>{{ user.is_active ? '已启用' : '已停用' }}</em>
                </label>
              </td>
              <td>{{ new Date(user.created_at).toLocaleDateString('zh-CN') }}</td>
              <td>
                <div class="row-actions">
                  <button class="icon-button" :disabled="savingUser === user.id" title="保存用户设置" aria-label="保存用户设置" @click="saveUser(user)">
                    <span v-if="savingUser === user.id" class="button-spinner" />
                    <Check v-else :size="17" />
                  </button>
                  <button class="icon-button" title="重置密码" aria-label="重置密码" @click="openResetModal(user)"><KeyRound :size="16" /></button>
                  <button class="icon-button icon-button--danger" title="删除用户" aria-label="删除用户" @click="confirmDeleteUser(user)"><Trash2 :size="16" /></button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <EmptyState v-if="!filteredUsers.length" title="暂无匹配用户" description="调整搜索条件或新增用户。" />
    </section>

    <!-- ==================== SETTINGS TAB ==================== -->
    <section v-else-if="activeTab === 'settings'" class="settings-grid">
      <div class="settings-toolbar">
        <button class="button button--secondary" :disabled="resettingSettings" @click="confirmResetSettings">
          <RotateCcw :size="15" />{{ resettingSettings ? '恢复中…' : '恢复默认值' }}
        </button>
      </div>
      <article v-for="setting in settings" :key="setting.key" :class="['setting-card', { 'card-success': settingSaveSuccess === setting.key }]">
        <div class="setting-card-heading"><span><Database :size="18" /></span><div><p class="section-label">{{ setting.key }}</p><strong>{{ setting.description || 'AI 运行参数' }}</strong></div></div>
        <select v-if="setting.key === 'default_language'" v-model="drafts[setting.key].value" class="field-control" aria-label="系统默认回答语言" @focus="snapshotSetting(setting.key)">
          <option value="zh-CN">中文</option><option value="en-US">English</option>
        </select>
        <select v-else-if="setting.key === 'reply_strategy'" v-model="drafts[setting.key].value" class="field-control" aria-label="系统默认回复策略" @focus="snapshotSetting(setting.key)">
          <option value="concise">简洁</option><option value="balanced">均衡</option><option value="detailed">详细</option>
        </select>
        <textarea v-else-if="drafts[setting.key]?.value.length > 100" v-model="drafts[setting.key].value" class="field-control setting-value" rows="5" :aria-label="`${setting.key} 的值`" @focus="snapshotSetting(setting.key)" />
        <input v-else v-model="drafts[setting.key].value" class="field-control" :aria-label="`${setting.key} 的值`" @focus="snapshotSetting(setting.key)" />
        <input v-model="drafts[setting.key].description" class="field-control setting-description" placeholder="配置说明" :aria-label="`${setting.key} 的说明`" />
        <button class="button button--secondary" :disabled="savingSetting === setting.key" @click="saveSetting(setting)">
          <span v-if="savingSetting === setting.key" class="button-spinner" />
          <Save v-else :size="16" />{{ savingSetting === setting.key ? '保存中' : '保存配置' }}
        </button>
      </article>
      <EmptyState v-if="!settings.length" title="暂无运行配置" description="后端返回配置项后，可在这里安全调整。" />
    </section>

    <!-- ==================== KNOWLEDGE TAB ==================== -->
    <section v-else-if="activeTab === 'knowledge'" class="admin-embedded-knowledge">
      <div class="admin-knowledge-upload-note"><span><Upload :size="17" /></span><div><strong>知识库文档上传</strong><p>支持 TXT、Markdown、CSV、PDF 和 DOCX；上传后会自动解析并建立检索索引。</p></div></div>
      <KnowledgeView />
    </section>

    <!-- ==================== ADMIN LOG TAB ==================== -->
    <section v-else-if="activeTab === 'adminlog'" class="admin-surface">
      <div class="section-heading">
        <div><p class="eyebrow">合规审计</p><h3>管理员操作日志</h3></div>
        <div class="section-heading-actions">
          <select v-model="adminLogsActionFilter" class="field-control filter-select" aria-label="按操作类型筛选" @change="adminLogsPage = 1; loadAdminLogs()">
            <option value="">全部操作</option>
            <option v-for="(label, key) in actionLabels" :key="key" :value="key">{{ label }}</option>
          </select>
          <button class="icon-button" title="刷新日志" aria-label="刷新日志" @click="loadAdminLogs"><RefreshCw :size="16" /></button>
        </div>
      </div>
      <LoadingState v-if="adminLogsLoading" label="正在加载操作日志" />
      <p v-else-if="adminLogsError" class="inline-error"><AlertCircle :size="16" />{{ adminLogsError }}</p>
      <template v-else-if="adminLogs.length">
        <div class="data-table-wrap">
          <table class="data-table">
            <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>目标</th><th>详情</th><th>结果</th></tr></thead>
            <tbody>
              <tr v-for="log in adminLogs" :key="log.id">
                <td class="log-time">{{ new Date(log.created_at).toLocaleString('zh-CN') }}</td>
                <td>{{ log.admin_name }}</td>
                <td><span class="log-action-badge">{{ actionLabels[log.action] || log.action }}</span></td>
                <td>{{ log.target_name || '-' }}</td>
                <td class="log-detail">{{ log.detail || '-' }}</td>
                <td><StatusBadge :value="log.success ? 'completed' : 'fallback'" type="trace" /></td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="admin-log-pagination">
          <button class="button button--secondary" :disabled="adminLogsPage <= 1" @click="adminLogsPage--; loadAdminLogs()">上一页</button>
          <span class="page-info">{{ adminLogsPage }} / {{ adminLogsTotalPages }}（共 {{ adminLogsTotal }} 条）</span>
          <button class="button button--secondary" :disabled="adminLogsPage >= adminLogsTotalPages" @click="adminLogsPage++; loadAdminLogs()">下一页</button>
        </div>
      </template>
      <EmptyState v-else title="暂无操作日志" description="管理员执行的操作将记录在此处。" />
    </section>

    <!-- ==================== REPORT TAB ==================== -->
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

    <!-- ==================== CONVERSATION AUDIT TAB ==================== -->
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

    <!-- ==================== CREATE USER MODAL ==================== -->
    <Teleport to="body">
      <div v-if="showCreateModal" class="modal-overlay" @click.self="showCreateModal = false">
        <div class="modal-panel" role="dialog" aria-modal="true" aria-label="新增用户">
          <div class="modal-header"><h3><Plus :size="18" />新增用户</h3><button class="icon-button" aria-label="关闭" @click="showCreateModal = false"><X :size="18" /></button></div>
          <div class="modal-body">
            <p v-if="createError" class="inline-error"><AlertCircle :size="14" />{{ createError }}</p>
            <label class="modal-field"><span>姓名</span><input v-model="createForm.display_name" class="field-control" placeholder="用户姓名" maxlength="80" /></label>
            <label class="modal-field"><span>邮箱</span><input v-model="createForm.email" type="email" class="field-control" placeholder="user@example.com" maxlength="255" /></label>
            <label class="modal-field"><span>密码（至少 8 位）</span><input v-model="createForm.password" type="password" class="field-control" placeholder="至少 8 位字符" maxlength="128" /></label>
            <label class="modal-field"><span>角色</span>
              <select v-model="createForm.role" class="field-control">
                <option v-for="role in roleOptions" :key="role.value" :value="role.value">{{ role.label }}</option>
              </select>
            </label>
            <label class="modal-field modal-field--inline"><span>立即启用</span><input v-model="createForm.is_active" type="checkbox" /></label>
          </div>
          <div class="modal-footer">
            <button class="button button--secondary" @click="showCreateModal = false">取消</button>
            <button class="button button--primary" :disabled="createSaving" @click="submitCreateUser">
              <span v-if="createSaving" class="button-spinner" />{{ createSaving ? '创建中…' : '确认创建' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ==================== RESET PASSWORD MODAL ==================== -->
    <Teleport to="body">
      <div v-if="showResetModal" class="modal-overlay" @click.self="showResetModal = false">
        <div class="modal-panel" role="dialog" aria-modal="true" aria-label="重置密码">
          <div class="modal-header"><h3><KeyRound :size="18" />重置密码 — {{ resetTarget?.display_name }}</h3><button class="icon-button" aria-label="关闭" @click="showResetModal = false"><X :size="18" /></button></div>
          <div class="modal-body">
            <p v-if="resetError" class="inline-error"><AlertCircle :size="14" />{{ resetError }}</p>
            <label class="modal-field"><span>新密码（至少 8 位）</span><input v-model="resetPassword" type="password" class="field-control" placeholder="输入新密码" maxlength="128" /></label>
            <p class="modal-hint">重置后旧密码立即失效，用户需使用新密码登录。</p>
          </div>
          <div class="modal-footer">
            <button class="button button--secondary" @click="showResetModal = false">取消</button>
            <button class="button button--primary" :disabled="resetSaving" @click="submitResetPassword">
              <span v-if="resetSaving" class="button-spinner" />{{ resetSaving ? '重置中…' : '确认重置' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- ==================== CONFIRM DIALOG ==================== -->
    <Teleport to="body">
      <div v-if="confirmDialog" class="modal-overlay" @click.self="confirmDialog = null">
        <div class="modal-panel modal-panel--sm" role="alertdialog" aria-modal="true" :aria-label="confirmDialog.title">
          <div class="modal-header"><h3>{{ confirmDialog.title }}</h3></div>
          <div class="modal-body"><p>{{ confirmDialog.message }}</p></div>
          <div class="modal-footer">
            <button class="button button--secondary" @click="confirmDialog = null">取消</button>
            <button class="button button--danger" @click="executeConfirm">确认</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<style scoped>
.admin-toasts { position: fixed; top: 1.2rem; right: 1.2rem; z-index: 9999; display: flex; flex-direction: column; gap: .5rem; pointer-events: none; }
.admin-toast { display: flex; align-items: center; gap: .5rem; padding: .65rem 1rem; border-radius: .5rem; font-size: .85rem; font-weight: 500; color: #fff; box-shadow: 0 4px 16px rgb(0 0 0 / .15); }
.admin-toast--success { background: #16a34a; }
.admin-toast--error { background: #dc2626; }
.toast-enter-active, .toast-leave-active { transition: all .3s ease; }
.toast-enter-from, .toast-leave-to { opacity: 0; transform: translateX(1rem); }

.section-heading-actions { display: flex; align-items: center; gap: .75rem; }
.admin-filter-bar { display: flex; gap: .75rem; margin-bottom: 1rem; flex-wrap: wrap; align-items: center; }
.filter-search { display: flex; align-items: center; gap: .4rem; flex: 1; min-width: 180px; max-width: 320px; padding: 0 .6rem; border: 1px solid var(--border, #e2e8f0); border-radius: .5rem; background: var(--surface, #fff); }
.filter-search input { border: none; outline: none; background: transparent; width: 100%; padding: .5rem 0; font-size: .85rem; }
.filter-select { width: auto; min-width: 110px; padding: .45rem .6rem; font-size: .83rem; }

.row-actions { display: flex; gap: .35rem; align-items: center; }
.icon-button--danger:hover { color: #dc2626; }
.row-success { background: rgb(22 163 74 / .07); transition: background .3s; }
.card-success { outline: 2px solid #16a34a; outline-offset: -2px; transition: outline .3s; }

.settings-toolbar { display: flex; justify-content: flex-end; margin-bottom: 1rem; grid-column: 1 / -1; }

.log-time { white-space: nowrap; font-size: .8rem; }
.log-detail { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: .82rem; }
.log-action-badge { display: inline-block; padding: .15rem .5rem; border-radius: .3rem; background: var(--accent-soft, #eff6ff); font-size: .78rem; font-weight: 500; }
.admin-log-pagination { display: flex; align-items: center; justify-content: center; gap: 1rem; margin-top: 1rem; }
.page-info { font-size: .83rem; color: var(--text-muted, #64748b); }

.modal-overlay { position: fixed; inset: 0; z-index: 8000; display: flex; align-items: center; justify-content: center; background: rgb(0 0 0 / .45); padding: 1rem; }
.modal-panel { background: var(--surface, #fff); border-radius: .75rem; width: 100%; max-width: 440px; box-shadow: 0 12px 40px rgb(0 0 0 / .2); }
.modal-panel--sm { max-width: 380px; }
.modal-header { display: flex; align-items: center; justify-content: space-between; padding: 1rem 1.25rem .5rem; }
.modal-header h3 { display: flex; align-items: center; gap: .5rem; font-size: 1rem; margin: 0; }
.modal-body { padding: .75rem 1.25rem; display: flex; flex-direction: column; gap: .75rem; }
.modal-field { display: flex; flex-direction: column; gap: .3rem; font-size: .85rem; }
.modal-field span { font-weight: 500; color: var(--text-muted, #64748b); }
.modal-field--inline { flex-direction: row; align-items: center; gap: .6rem; }
.modal-hint { font-size: .78rem; color: var(--text-muted, #64748b); margin: 0; }
.modal-footer { display: flex; justify-content: flex-end; gap: .6rem; padding: .75rem 1.25rem 1.25rem; }
.button--danger { background: #dc2626; color: #fff; border: none; }
.button--danger:hover { background: #b91c1c; }
</style>
