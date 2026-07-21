<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  AlertCircle,
  CheckCircle2,
  FilePlus2,
  FileText,
  FolderUp,
  Pencil,
  Plus,
  RefreshCw,
  Save,
  Search,
  Trash2,
  TriangleAlert,
  Upload,
  X,
} from 'lucide-vue-next'
import { errorMessage, knowledgeApi } from '@/api/client'
import EmptyState from '@/components/EmptyState.vue'
import LoadingState from '@/components/LoadingState.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import type { Citation, KnowledgeDocument } from '@/types'

const documents = ref<KnowledgeDocument[]>([])
const loading = ref(false)
const error = ref('')
const searchQuery = ref('')
const searchLoading = ref(false)
const results = ref<Citation[]>([])
const searchError = ref('')
const showEditor = ref(false)
const editingDocument = ref<KnowledgeDocument | null>(null)
const saving = ref(false)
const editorError = ref('')
const uploadError = ref('')
const uploading = ref(false)
const actionError = ref('')
const actionSuccess = ref('')
const reindexingId = ref<number | null>(null)
const deleteTarget = ref<KnowledgeDocument | null>(null)
const deleting = ref(false)
const deleteError = ref('')
const emptyForm = () => ({ title: '', source: '手工录入', content: '' })
const form = ref(emptyForm())
const filteredDocuments = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  return !query ? documents.value : documents.value.filter((document) => `${document.title}${document.source}${document.content}`.toLowerCase().includes(query))
})

async function loadDocuments() {
  loading.value = true
  error.value = ''
  try { documents.value = await knowledgeApi.documents() } catch (caught) { error.value = errorMessage(caught) } finally { loading.value = false }
}

async function searchKnowledge() {
  const query = searchQuery.value.trim()
  if (!query) { results.value = []; return }
  searchLoading.value = true
  searchError.value = ''
  try { results.value = (await knowledgeApi.search({ query, top_k: 5 })).results } catch (caught) { searchError.value = errorMessage(caught) } finally { searchLoading.value = false }
}

function openCreate() {
  editingDocument.value = null
  form.value = emptyForm()
  editorError.value = ''
  showEditor.value = true
}

function openEdit(document: KnowledgeDocument) {
  editingDocument.value = document
  form.value = { title: document.title, source: document.source, content: document.content }
  editorError.value = ''
  showEditor.value = true
}

function closeEditor() {
  if (saving.value) return
  showEditor.value = false
}

function replaceDocument(updated: KnowledgeDocument) {
  documents.value = [updated, ...documents.value.filter((document) => document.id !== updated.id)]
}

async function saveDocument() {
  saving.value = true
  editorError.value = ''
  actionError.value = ''
  actionSuccess.value = ''
  try {
    const document = editingDocument.value
      ? await knowledgeApi.update(editingDocument.value.id, form.value)
      : await knowledgeApi.create(form.value)
    replaceDocument(document)
    results.value = []
    actionSuccess.value = editingDocument.value ? `“${document.title}”已更新并重建索引。` : `“${document.title}”已入库。`
    showEditor.value = false
  } catch (caught) { editorError.value = errorMessage(caught) } finally { saving.value = false }
}

async function uploadFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  uploadError.value = ''
  try {
    const document = await knowledgeApi.upload(file)
    documents.value.unshift(document)
  } catch (caught) { uploadError.value = errorMessage(caught) } finally {
    uploading.value = false
    input.value = ''
  }
}

async function reindexDocument(document: KnowledgeDocument) {
  reindexingId.value = document.id
  actionError.value = ''
  actionSuccess.value = ''
  try {
    const result = await knowledgeApi.reindex(document.id)
    replaceDocument(result.document)
    results.value = []
    actionSuccess.value = `“${result.document.title}”索引已重建，共生成 ${result.indexed_chunks} 个分块。`
  } catch (caught) { actionError.value = errorMessage(caught) } finally { reindexingId.value = null }
}

function openDelete(document: KnowledgeDocument) {
  deleteTarget.value = document
  deleteError.value = ''
}

function closeDelete() {
  if (deleting.value) return
  deleteTarget.value = null
}

async function confirmDelete() {
  if (!deleteTarget.value) return
  const document = deleteTarget.value
  deleting.value = true
  deleteError.value = ''
  actionError.value = ''
  actionSuccess.value = ''
  try {
    await knowledgeApi.remove(document.id)
    documents.value = documents.value.filter((item) => item.id !== document.id)
    results.value = results.value.filter((item) => item.document_id !== document.id)
    deleteTarget.value = null
    actionSuccess.value = `“${document.title}”已删除。`
  } catch (caught) { deleteError.value = errorMessage(caught) } finally { deleting.value = false }
}

onMounted(() => { void loadDocuments() })
</script>

<template>
  <div class="knowledge-page">
    <section class="page-toolbar">
      <div><p class="eyebrow">企业资料中心</p><h2>让每个回答都可追溯</h2><p>管理可供 AI 检索的业务规则、服务手册和标准流程。</p></div>
      <div class="toolbar-actions"><label class="button button--secondary" :class="{ 'is-disabled': uploading }"><Upload :size="17" />{{ uploading ? '正在上传' : '上传文件' }}<input type="file" accept=".txt,.md,.csv,.pdf,.docx" hidden :disabled="uploading" @change="uploadFile" /></label><button class="button button--primary" @click="openCreate"><Plus :size="17" />新建资料</button></div>
    </section>
    <p v-if="uploadError" class="inline-error"><AlertCircle :size="16" />{{ uploadError }}</p>
    <p v-if="actionError" class="inline-error"><AlertCircle :size="16" />{{ actionError }}</p>
    <p v-if="actionSuccess" class="inline-success"><CheckCircle2 :size="16" />{{ actionSuccess }}</p>

    <section class="knowledge-search">
      <Search :size="19" /><input v-model="searchQuery" placeholder="搜索标题、来源或检索测试问题" @keyup.enter="searchKnowledge" /><button class="button button--primary" :disabled="searchLoading || !searchQuery.trim()" @click="searchKnowledge">{{ searchLoading ? '检索中' : '检索' }}</button>
    </section>
    <p v-if="searchError" class="inline-error"><AlertCircle :size="16" />{{ searchError }}</p>
    <section v-if="results.length" class="search-results"><div class="section-heading"><div><p class="eyebrow">检索证据</p><h3>知识库命中结果</h3></div><button class="icon-button" title="关闭检索结果" aria-label="关闭检索结果" @click="results = []"><X :size="17" /></button></div><article v-for="result in results" :key="result.document_id" class="search-result"><div><FileText :size="18" /><strong>{{ result.title }}</strong></div><p>{{ result.excerpt }}</p><small>相似度 {{ Math.round(result.score * 100) }}%</small></article></section>

    <section class="document-table-section">
      <div class="section-heading"><div><p class="eyebrow">已入库资料</p><h3>{{ documents.length }} 份文档</h3></div></div>
      <LoadingState v-if="loading" label="正在获取知识资料" />
      <p v-else-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
      <div v-else class="document-table-wrap">
        <table class="data-table">
          <thead><tr><th>资料名称</th><th>来源</th><th>状态</th><th>最近更新</th><th class="document-actions-heading">操作</th></tr></thead>
          <tbody><tr v-for="document in filteredDocuments" :key="document.id"><td><div class="document-name"><span><FileText :size="18" /></span><div><strong>{{ document.title }}</strong><small>{{ document.content.slice(0, 72) }}{{ document.content.length > 72 ? '...' : '' }}</small></div></div></td><td>{{ document.source }}</td><td><StatusBadge :value="document.status" /></td><td>{{ new Date(document.updated_at).toLocaleDateString('zh-CN') }}</td><td><div class="document-actions"><button class="icon-button" title="编辑文档" aria-label="编辑文档" :disabled="reindexingId !== null" @click="openEdit(document)"><Pencil :size="16" /></button><button class="icon-button" title="重建索引" aria-label="重建索引" :disabled="reindexingId !== null" @click="reindexDocument(document)"><RefreshCw :size="16" :class="{ 'is-spinning': reindexingId === document.id }" /></button><button class="icon-button icon-button--danger" title="删除文档" aria-label="删除文档" :disabled="reindexingId !== null" @click="openDelete(document)"><Trash2 :size="16" /></button></div></td></tr></tbody>
        </table>
        <EmptyState v-if="!filteredDocuments.length" title="暂无匹配资料" description="调整关键词，或新建一份企业资料供 AI 检索。"><button class="button button--secondary" @click="openCreate"><FilePlus2 :size="16" />新建资料</button></EmptyState>
      </div>
    </section>

    <div v-if="showEditor" class="modal-backdrop" role="presentation" @click.self="closeEditor">
      <form class="modal modal--wide" @submit.prevent="saveDocument">
        <div class="modal-header"><div><p class="eyebrow">知识资料</p><h2>{{ editingDocument ? '编辑资料' : '新建资料' }}</h2></div><button class="icon-button" type="button" title="关闭" aria-label="关闭" :disabled="saving" @click="closeEditor"><X :size="19" /></button></div>
        <label class="field-label" for="document-title">资料名称</label><input id="document-title" v-model="form.title" class="field-control" required minlength="2" maxlength="255" />
        <label class="field-label" for="document-source">资料来源</label><input id="document-source" v-model="form.source" class="field-control" required maxlength="255" />
        <label class="field-label" for="document-content">正文内容</label><textarea id="document-content" v-model="form.content" class="field-control document-editor" rows="12" required minlength="20" maxlength="100000" />
        <p v-if="editorError" class="form-error">{{ editorError }}</p><button class="button button--primary button--wide" :disabled="saving" type="submit"><component :is="editingDocument ? Save : FolderUp" :size="17" />{{ saving ? '正在保存并建索引' : editingDocument ? '保存并重建索引' : '保存并建立索引' }}</button>
      </form>
    </div>

    <div v-if="deleteTarget" class="modal-backdrop" role="presentation" @click.self="closeDelete">
      <section class="modal delete-confirmation" role="alertdialog" aria-modal="true" aria-labelledby="delete-document-title">
        <div class="modal-header"><div><p class="eyebrow">删除知识资料</p><h2 id="delete-document-title">确认删除文档</h2></div><button class="icon-button" type="button" title="关闭" aria-label="关闭" :disabled="deleting" @click="closeDelete"><X :size="19" /></button></div>
        <div class="delete-warning"><TriangleAlert :size="20" /><p>将永久删除 <strong>“{{ deleteTarget.title }}”</strong> 及其全部索引分块。此操作不可撤销。</p></div>
        <p v-if="deleteError" class="form-error">{{ deleteError }}</p>
        <div class="modal-actions"><button class="button button--quiet" type="button" :disabled="deleting" @click="closeDelete">取消</button><button class="button button--danger" type="button" :disabled="deleting" @click="confirmDelete"><Trash2 :size="16" />{{ deleting ? '正在删除' : '确认删除' }}</button></div>
      </section>
    </div>
  </div>
</template>
