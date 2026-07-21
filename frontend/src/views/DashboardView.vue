<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { AlertCircle, ArrowUpRight, Bell, Bot, Check, CircleGauge, Database, MessageCircle, RefreshCw, Send, ShieldAlert, ShieldCheck, TicketCheck, UserCheck, X } from 'lucide-vue-next'
import type { Component } from 'vue'
import { dashboardApi, errorMessage, executiveApi } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import MetricCard from '@/components/MetricCard.vue'
import type { AdminConversationDetail, DashboardDetail, DashboardDetailRow, DashboardDetailScope, DashboardOverview, DashboardReport, SupportAgent } from '@/types'

const overview = ref<DashboardOverview>()
const report = ref<DashboardReport>()
const loading = ref(false)
const error = ref('')
const selectedDetail = ref<DashboardDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref('')
const detailConversation = ref<AdminConversationDetail | null>(null)
const supportAgents = ref<SupportAgent[]>([])
const supportAgentsLoading = ref(false)
const supportAgentsError = ref('')
const takeoverAgentId = ref<number>()
const takeoverNotice = ref('')
const takeoverLoading = ref(false)
const notifyLoading = ref(false)
const takeoverError = ref('')
const takeoverSuccess = ref('')
const executiveReply = ref('')
const executiveSending = ref(false)
const executiveReplyError = ref('')
const metricIcons: Component[] = [MessageCircle, TicketCheck, ShieldCheck, Database]

const maxCategory = computed(() => Math.max(1, ...(overview.value?.category_distribution.map((item) => item.value) ?? [1])))
const feedbackTrend = computed(() => overview.value?.feedback_satisfaction_trend ?? [])
const maxTrend = computed(() => Math.max(1, ...(feedbackTrend.value.map((item) => item.value) ?? [1])))
const ticketSummary = computed(() => overview.value?.ticket_statuses ?? overview.value?.ticket_counts ?? overview.value?.ticket_summary ?? {})
const metricNumber = (labels: string[]) => {
  const metric = overview.value?.metrics.find((item) => labels.some((label) => item.label.includes(label)))
  return metric?.value ?? '--'
}
const ticketStatusCards = computed(() => [
  { label: '工单总量', value: ticketSummary.value.total ?? metricNumber(['工单总量', '累计工单']), tone: 'teal' as const, status: undefined },
  { label: '待处理', value: ticketSummary.value.pending ?? ticketSummary.value.open ?? metricNumber(['待处理工单']), tone: 'coral' as const, status: 'pending' },
  { label: '处理中', value: ticketSummary.value.in_progress ?? metricNumber(['处理中']), tone: 'blue' as const, status: 'in_progress' },
  { label: '已解决', value: ticketSummary.value.resolved ?? metricNumber(['已解决']), tone: 'teal' as const, status: 'resolved' },
  { label: '紧急工单', value: ticketSummary.value.urgent ?? overview.value?.urgent_tickets ?? metricNumber(['紧急']), tone: 'gold' as const, priority: 'urgent' },
])

function iconForMetric(index: number): Component { return metricIcons[index % metricIcons.length] }
function chartWidth(value: number) { return `${Math.max(4, Math.round((value / maxCategory.value) * 100))}%` }
function chartHeight(value: number) { return `${Math.max(8, Math.round((value / maxTrend.value) * 100))}%` }

function detailRowTitle(row: DashboardDetailRow) {
  return String(row.title ?? row.customer_name ?? row.label ?? row.question ?? row.text ?? (row.id ? `记录 #${row.id}` : '明细记录'))
}

function detailRowContent(row: DashboardDetailRow) {
  return String(row.content ?? row.question ?? row.summary ?? row.detail ?? row.text ?? '')
}

function detailRowMeta(row: DashboardDetailRow) {
  return [
    row.category,
    row.status,
    row.priority,
    typeof row.rating === 'number' ? `评分 ${row.rating}/5` : undefined,
    typeof row.helpful === 'boolean' ? (row.helpful ? '有帮助' : '没有帮助') : undefined,
    typeof row.quality_score === 'number' ? `质检 ${Math.round(row.quality_score * 100)}%` : undefined,
    row.created_at ? new Date(String(row.created_at)).toLocaleString('zh-CN') : undefined,
  ].filter(Boolean).join(' · ')
}

async function openDetail(scope: DashboardDetailScope, params: Record<string, string | number> = {}) {
  detailConversation.value = null
  selectedDetail.value = { scope, title: '正在载入明细', summary: '', rows: [] }
  detailLoading.value = true
  detailError.value = ''
  try {
    selectedDetail.value = await dashboardApi.details(scope, params)
  } catch (caught) {
    detailError.value = errorMessage(caught)
  } finally {
    detailLoading.value = false
  }
}

async function openConversationDetail(conversationId: number) {
  detailLoading.value = true
  detailError.value = ''
  takeoverError.value = ''
  takeoverSuccess.value = ''
  takeoverAgentId.value = undefined
  takeoverNotice.value = ''
  executiveReply.value = ''
  executiveReplyError.value = ''
  try {
    const [conversation, metadata] = await Promise.all([
      dashboardApi.conversation(conversationId),
      executiveApi.conversation(conversationId),
      loadSupportAgents(),
    ])
    detailConversation.value = { ...conversation, ...metadata }
    takeoverAgentId.value = metadata.assigned_agent?.id ?? metadata.assigned_agent_id ?? undefined
  } catch (caught) {
    detailError.value = errorMessage(caught)
  } finally {
    detailLoading.value = false
  }
}

async function loadSupportAgents() {
  if (supportAgents.value.length || supportAgentsLoading.value) return
  supportAgentsLoading.value = true
  supportAgentsError.value = ''
  try {
    supportAgents.value = await executiveApi.supportAgents()
  } catch (caught) {
    supportAgentsError.value = errorMessage(caught)
  } finally {
    supportAgentsLoading.value = false
  }
}

function conversationAgentLabel() {
  const conversation = detailConversation.value
  if (!conversation) return '尚未分配客服'
  return conversation.assigned_agent?.display_name || (conversation.assigned_agent_id ? `客服 #${conversation.assigned_agent_id}` : '尚未分配客服')
}

function isExecutiveControlled(conversation: AdminConversationDetail | null) {
  return conversation?.control_mode === 'executive_takeover' || conversation?.takeover_by_id != null
}

async function forceTakeover() {
  const conversation = detailConversation.value
  const agentId = takeoverAgentId.value
  if (!conversation || !agentId || takeoverLoading.value || notifyLoading.value) return
  takeoverLoading.value = true
  takeoverError.value = ''
  takeoverSuccess.value = ''
  try {
    const updated = await executiveApi.takeoverConversation(conversation.id, {
      assigned_agent_id: agentId,
      ...(takeoverNotice.value.trim() ? { notice: takeoverNotice.value.trim() } : {}),
    })
    detailConversation.value = { ...conversation, ...updated }
    takeoverSuccess.value = `已强制接管并通知${supportAgents.value.find((agent) => agent.id === agentId)?.display_name || `客服 #${agentId}`}`
  } catch (caught) {
    takeoverError.value = errorMessage(caught)
  } finally {
    takeoverLoading.value = false
  }
}

async function sendManagerNotice() {
  const conversation = detailConversation.value
  const agentId = takeoverAgentId.value
  const message = takeoverNotice.value.trim()
  if (!conversation || !agentId || !message || takeoverLoading.value || notifyLoading.value) return
  notifyLoading.value = true
  takeoverError.value = ''
  takeoverSuccess.value = ''
  try {
    await executiveApi.notifyConversation(conversation.id, { assigned_agent_id: agentId, notice: message })
    takeoverSuccess.value = `通知已发送给${supportAgents.value.find((agent) => agent.id === agentId)?.display_name || `客服 #${agentId}`}`
  } catch (caught) {
    takeoverError.value = errorMessage(caught)
  } finally {
    notifyLoading.value = false
  }
}

async function sendExecutiveReply() {
  const conversation = detailConversation.value
  const content = executiveReply.value.trim()
  if (!conversation || !content || executiveSending.value || !isExecutiveControlled(conversation)) return
  executiveSending.value = true
  executiveReplyError.value = ''
  try {
    const message = await executiveApi.sendMessage(conversation.id, content)
    detailConversation.value = { ...conversation, messages: [...conversation.messages, message] }
    executiveReply.value = ''
  } catch (caught) {
    executiveReplyError.value = errorMessage(caught)
  } finally {
    executiveSending.value = false
  }
}

function closeDetail() {
  selectedDetail.value = null
  detailConversation.value = null
  detailError.value = ''
  takeoverError.value = ''
  takeoverSuccess.value = ''
  executiveReply.value = ''
  executiveReplyError.value = ''
}

function handlePanelKey(event: KeyboardEvent, callback: () => void) {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault()
    callback()
  }
}

function metricDetailScope(label: string): DashboardDetailScope {
  if (label.includes('咨询')) return 'consultations'
  if (label.includes('质检') || label.includes('满意')) return 'satisfaction'
  if (label.includes('工单')) return 'tickets'
  return 'insights'
}

function metricDetailParams(label: string): Record<string, string> {
  if (label.includes('待处理')) return { status: 'pending' }
  return {}
}

async function loadDashboard() {
  loading.value = true
  error.value = ''
  try {
    const [nextOverview, nextReport] = await Promise.all([dashboardApi.overview(), dashboardApi.report()])
    overview.value = nextOverview
    report.value = nextReport
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    loading.value = false
  }
}

onMounted(() => { void loadDashboard() })
</script>

<template>
  <div class="dashboard-page">
    <section class="page-toolbar">
      <div><p class="eyebrow">服务运营总览</p><h2>今天的业务响应，清晰可见。</h2><p>聚合咨询处理、知识检索、客服工单与服务质量信号。</p></div>
      <button class="icon-button" title="刷新运营数据" aria-label="刷新运营数据" @click="loadDashboard"><RefreshCw :size="18" /></button>
    </section>
    <LoadingState v-if="loading" label="正在汇总运营数据" />
    <p v-else-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
    <template v-else-if="overview">
      <section class="metrics-grid"><MetricCard v-for="(metric, index) in overview.metrics" :key="metric.label" :label="metric.label" :value="metric.label.includes('咨询') && overview.consultation_count !== undefined ? overview.consultation_count : metric.value" :detail="metric.delta" :tone="metric.tone" :icon="iconForMetric(index)" interactive :aria-label="`查看${metric.label}明细`" @click="openDetail(metricDetailScope(metric.label), metricDetailParams(metric.label))" /></section>
      <section class="ticket-status-board" aria-label="工单状态统计"><div class="section-heading"><div><p class="eyebrow">工单总览</p><h3>工单状态与紧急程度</h3></div><span class="chart-legend">点击查看明细</span></div><div class="ticket-status-grid"><MetricCard v-for="(item, index) in ticketStatusCards" :key="item.label" :label="item.label" :value="item.value" detail="点击查看具体记录" :tone="item.tone" :icon="iconForMetric(index + 1)" interactive :aria-label="`查看${item.label}工单`" @click="openDetail('tickets', item.status ? { status: item.status } : item.priority ? { priority: item.priority } : {})" /></div></section>
      <section class="dashboard-grid">
        <article class="analytics-panel analytics-panel--wide dashboard-panel-clickable" role="button" tabindex="0" aria-label="查看问题分类明细" @click="openDetail('category')" @keydown="handlePanelKey($event, () => openDetail('category'))"><div class="section-heading"><div><p class="eyebrow">咨询构成</p><h3>业务意图分布</h3></div><span class="chart-legend">点击查看明细</span></div><div v-if="overview.category_distribution.length" class="bar-chart"><button v-for="item in overview.category_distribution" :key="item.name" type="button" class="bar-chart-row dashboard-bar-row" :aria-label="`查看${item.name}分类明细`" @click.stop="openDetail('tickets', { category: item.name })"><span>{{ item.name }}</span><div class="bar-chart-track"><i :style="{ width: chartWidth(item.value) }" /></div><strong>{{ item.value }}</strong></button></div><EmptyState v-else title="暂无分类数据" description="产生咨询记录后，将展示业务意图分布。" /></article>
        <article class="analytics-panel"><div class="section-heading"><div><p class="eyebrow">系统状态</p><h3>AI 服务链路</h3></div><CircleGauge :size="20" /></div><div class="system-status-list"><div><span class="status-dot" /><p><small>模型提供方</small><strong>{{ overview.system.provider }}</strong></p></div><div><span class="status-dot status-dot--gold" /><p><small>Dify 工作流</small><strong>{{ overview.system.dify }}</strong></p></div><div><span class="status-dot status-dot--coral" /><p><small>知识索引</small><strong>{{ overview.system.index }}</strong></p></div></div></article>
        <article class="analytics-panel analytics-panel--wide dashboard-panel-clickable" role="button" tabindex="0" aria-label="查看 AI 回复满意度明细" @click="openDetail('satisfaction')" @keydown="handlePanelKey($event, () => openDetail('satisfaction'))"><div class="section-heading"><div><p class="eyebrow">服务质量</p><h3>AI 回复满意度</h3></div><span class="chart-legend">点击查看记录</span></div><p v-if="overview.feedback_count" class="dashboard-feedback-summary">基于 {{ overview.feedback_count }} 条用户评价 · 有帮助率 {{ overview.feedback_helpful_rate ?? '--' }}%</p><div v-if="feedbackTrend.length" class="trend-chart"><div v-for="item in feedbackTrend" :key="item.date" class="trend-column"><span>{{ item.value }}%</span><i :style="{ height: chartHeight(item.value) }" /><small>{{ item.date }}</small></div></div><p v-else-if="overview.feedback_count" class="report-summary">当前满意度 {{ overview.actual_ai_reply_satisfaction ?? '--' }}%</p><EmptyState v-else title="暂无用户满意度" description="企业用户结束会话并提交评价后，这里会形成真实满意度曲线。" /></article>
        <article class="analytics-panel dashboard-panel-clickable" role="button" tabindex="0" aria-label="查看运营洞察明细" @click="openDetail('insights')" @keydown="handlePanelKey($event, () => openDetail('insights'))"><div class="section-heading"><div><p class="eyebrow">运营洞察</p><h3>{{ report?.title || '本期重点' }}</h3></div><Bot :size="20" /></div><p class="report-summary">{{ report?.summary || '暂未生成运营报告。' }}</p><ul class="insight-list"><li v-for="insight in overview.insights" :key="insight">{{ insight }}</li></ul></article>
      </section>
    </template>
    <EmptyState v-else title="暂无运营数据" description="服务启动并产生业务记录后，仪表盘将在这里汇总关键指标。" />
  </div>

  <div v-if="selectedDetail" class="modal-backdrop dashboard-detail-backdrop" role="presentation" @click.self="closeDetail">
    <section class="modal modal--wide dashboard-detail-modal" role="dialog" aria-modal="true" :aria-label="selectedDetail.title">
      <div class="modal-header"><div><p class="eyebrow">经营数据明细</p><h2>{{ detailConversation ? detailConversation.title : selectedDetail.title }}</h2></div><button class="icon-button" type="button" title="关闭明细" aria-label="关闭明细" @click="closeDetail"><X :size="19" /></button></div>
      <LoadingState v-if="detailLoading" compact label="正在载入明细" />
      <p v-else-if="detailError" class="inline-error"><AlertCircle :size="15" />{{ detailError }}</p>
      <template v-else-if="detailConversation">
        <p class="dashboard-detail-summary">{{ detailConversation.customer_name || `企业用户 #${detailConversation.user_id ?? detailConversation.id}` }} · {{ detailConversation.messages.length }} 条消息</p>
        <section class="executive-takeover-panel" aria-label="管理者会话接管">
          <div class="executive-takeover-heading">
            <div><p class="eyebrow">管理者控制</p><h3><ShieldAlert :size="16" />强制接管与客服通知</h3></div>
            <span class="executive-current-assignee"><UserCheck :size="14" />{{ conversationAgentLabel() }}</span>
          </div>
          <p class="executive-takeover-help">接管后会话进入指定客服的实时队列；客服端只接收分配和通知，不可自行转派。</p>
          <div class="executive-takeover-fields">
            <label class="field-label" for="takeover-agent">指定客服</label>
            <select id="takeover-agent" v-model="takeoverAgentId" class="field-control" :disabled="supportAgentsLoading || takeoverLoading || notifyLoading">
              <option :value="undefined">请选择客服</option>
              <option v-for="agent in supportAgents" :key="agent.id" :value="agent.id" :disabled="!agent.is_active">{{ agent.display_name }}{{ agent.is_active ? '' : '（已停用）' }}</option>
            </select>
            <p v-if="supportAgentsLoading" class="field-hint">正在加载客服名单…</p>
            <p v-else-if="supportAgentsError" class="form-error">{{ supportAgentsError }}</p>
            <label class="field-label" for="takeover-notice">通知内容</label>
            <textarea id="takeover-notice" v-model="takeoverNotice" class="field-control executive-takeover-notice" rows="2" maxlength="500" placeholder="例如：请优先处理付款咨询，并在回复前核对工单。" :disabled="takeoverLoading || notifyLoading" />
          </div>
          <div class="executive-takeover-actions">
            <button class="button button--primary" type="button" :disabled="!takeoverAgentId || takeoverLoading || notifyLoading" @click="forceTakeover"><Check :size="15" />{{ takeoverLoading ? '接管中' : '强制接管并通知' }}</button>
            <button class="button button--secondary" type="button" :disabled="!takeoverAgentId || !takeoverNotice.trim() || takeoverLoading || notifyLoading" @click="sendManagerNotice"><Bell :size="15" />{{ notifyLoading ? '发送中' : '仅发送通知' }}</button>
          </div>
          <p v-if="takeoverError" class="inline-error"><AlertCircle :size="14" />{{ takeoverError }}</p>
          <p v-if="takeoverSuccess" class="inline-success"><Send :size="14" />{{ takeoverSuccess }}</p>
        </section>
        <div class="dashboard-conversation-messages"><article v-for="message in detailConversation.messages" :key="message.id" class="dashboard-conversation-message"><div><strong>{{ message.sender_label || message.role }}</strong><time>{{ message.created_at ? new Date(message.created_at).toLocaleString('zh-CN') : '时间未知' }}</time></div><p>{{ message.content }}</p></article></div>
        <form v-if="isExecutiveControlled(detailConversation)" class="executive-reply-composer" @submit.prevent="sendExecutiveReply">
          <div class="section-heading"><div><p class="eyebrow">当前接管权限</p><h3>经营管理者回复</h3></div><span class="chart-legend">客服端仅接收通知</span></div>
          <textarea v-model="executiveReply" class="field-control" rows="3" maxlength="4000" placeholder="输入要发送给企业用户的回复…" :disabled="executiveSending" />
          <div class="executive-reply-actions"><p v-if="executiveReplyError" class="inline-error"><AlertCircle :size="14" />{{ executiveReplyError }}</p><button class="button button--primary" type="submit" :disabled="executiveSending || !executiveReply.trim()"><Send :size="15" />{{ executiveSending ? '发送中' : '发送给企业用户' }}</button></div>
        </form>
      </template>
      <template v-else>
        <p class="dashboard-detail-summary">{{ selectedDetail.summary }}</p>
        <div v-if="selectedDetail.rows.length" class="dashboard-detail-rows"><button v-for="row in selectedDetail.rows" :key="String(row.id ?? row.title ?? row.label ?? row.text)" class="dashboard-detail-row" type="button" :disabled="typeof row.conversation_id !== 'number'" @click="typeof row.conversation_id === 'number' && openConversationDetail(row.conversation_id)"><span><strong>{{ detailRowTitle(row) }}</strong><small>{{ detailRowMeta(row) }}</small></span><p>{{ detailRowContent(row) }}</p><ArrowUpRight v-if="typeof row.conversation_id === 'number'" :size="16" /></button></div>
        <EmptyState v-else title="暂无明细记录" description="当前筛选条件下没有可展开的记录。" />
      </template>
    </section>
  </div>
</template>
