<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { AlertCircle, CheckCircle2, Download, Image as ImageIcon, LoaderCircle, Sparkles, X } from 'lucide-vue-next'
import { difyApi, errorMessage } from '@/api/client'
import type { DifyMediaResponse } from '@/types'

type ImageSize = '2048*2048' | '2688*1536' | '1536*2688'

const sizeOptions: Array<{ value: ImageSize; label: string; description: string }> = [
  { value: '2048*2048', label: '2048 × 2048', description: '正方形' },
  { value: '2688*1536', label: '2688 × 1536', description: '横向' },
  { value: '1536*2688', label: '1536 × 2688', description: '竖向' },
]

const prompt = ref('')
const size = ref<ImageSize>('2048*2048')
const pending = ref(false)
const error = ref('')
const success = ref('')
const generated = ref<{ source: string; response: DifyMediaResponse; size: ImageSize } | null>(null)
let requestController: AbortController | undefined
let imageObjectUrl = ''

const promptLength = computed(() => prompt.value.length)
const selectedSize = computed(() => sizeOptions.find((option) => option.value === size.value) ?? sizeOptions[0])
const generatedSizeLabel = computed(() => sizeOptions.find((option) => option.value === generated.value?.size)?.label ?? '')

function isCanceled(errorValue: unknown) {
  return typeof errorValue === 'object'
    && errorValue !== null
    && 'code' in errorValue
    && (errorValue as { code?: unknown }).code === 'ERR_CANCELED'
}

function releaseImageObjectUrl() {
  if (!imageObjectUrl) return
  URL.revokeObjectURL(imageObjectUrl)
  imageObjectUrl = ''
}

function formatBytes(value?: number | null) {
  if (!value) return '大小未知'
  if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function imageExtension(contentType: string) {
  const subtype = contentType.split('/')[1]?.split(';')[0]?.toLowerCase()
  return subtype === 'jpeg' ? 'jpg' : subtype || 'png'
}

async function generateImage() {
  const cleanPrompt = prompt.value.trim()
  if (!cleanPrompt) {
    error.value = '请输入图片描述。'
    success.value = ''
    return
  }

  requestController?.abort()
  const controller = new AbortController()
  requestController = controller
  pending.value = true
  error.value = ''
  success.value = ''
  let createdObjectUrl = ''

  try {
    const result = await difyApi.textToImage({ prompt: cleanPrompt, size: size.value }, { signal: controller.signal })
    if (result.kind !== 'image' || !result.content_type.startsWith('image/')) {
      throw new Error('文生图服务返回的媒体类型无法验证。')
    }

    let source = result.data_url || ''
    if (source && !source.startsWith('data:image/')) {
      throw new Error('文生图服务返回了无效的图片数据。')
    }
    if (!source && result.media_url) {
      const media = await difyApi.mediaProxy({ url: result.media_url, kind: 'image' }, { signal: controller.signal })
      if (!media.type.startsWith('image/')) throw new Error('图片媒体类型校验失败。')
      createdObjectUrl = URL.createObjectURL(media)
      source = createdObjectUrl
    }
    if (!source) throw new Error('文生图服务未返回可展示的真实图片。')

    releaseImageObjectUrl()
    imageObjectUrl = createdObjectUrl
    generated.value = { source, response: result, size: size.value }
    success.value = `已生成 ${selectedSize.value.label} 图片。`
  } catch (caught) {
    if (!controller.signal.aborted && !isCanceled(caught)) error.value = errorMessage(caught)
    if (createdObjectUrl && imageObjectUrl !== createdObjectUrl) URL.revokeObjectURL(createdObjectUrl)
  } finally {
    if (requestController === controller) requestController = undefined
    pending.value = false
  }
}

function cancelGeneration() {
  requestController?.abort()
  pending.value = false
}

function clearResult() {
  releaseImageObjectUrl()
  generated.value = null
  success.value = ''
}

function downloadImage() {
  const result = generated.value
  if (!result) return
  const link = document.createElement('a')
  link.href = result.source
  link.download = `business-ai-image-${Date.now()}.${imageExtension(result.response.content_type)}`
  document.body.appendChild(link)
  link.click()
  link.remove()
}

onBeforeUnmount(() => {
  requestController?.abort()
  releaseImageObjectUrl()
})
</script>

<template>
  <div class="image-studio-page">
    <section class="page-toolbar">
      <div>
        <p class="eyebrow">视觉素材工作台</p>
        <h2>文字转图片</h2>
        <p>生成客服沟通、系统内容和经营汇报所需的商务视觉素材。</p>
      </div>
      <div class="toolbar-actions">
        <span class="badge badge--ready"><Sparkles :size="13" />Dify 文生图</span>
      </div>
    </section>

    <div class="image-studio-layout">
      <section class="image-studio-controls" aria-labelledby="image-controls-title">
        <div class="section-heading">
          <div>
            <p class="section-label">生成参数</p>
            <h3 id="image-controls-title">描述你要的画面</h3>
          </div>
          <ImageIcon :size="19" class="image-studio-heading-icon" />
        </div>

        <label class="field-label" for="image-prompt">图片描述</label>
        <textarea
          id="image-prompt"
          v-model="prompt"
          class="field-control image-prompt-input"
          maxlength="2000"
          placeholder="例如：现代企业客服中心，明亮办公环境，真实摄影风格"
          rows="7"
        />
        <div class="image-prompt-meta"><span>支持商务场景描述</span><span>{{ promptLength }}/2000</span></div>

        <label class="field-label" for="image-size">画幅尺寸</label>
        <select id="image-size" v-model="size" class="field-control">
          <option v-for="option in sizeOptions" :key="option.value" :value="option.value">
            {{ option.label }} · {{ option.description }}
          </option>
        </select>
        <p class="field-hint">选择画幅后提交，生成结果会显示在右侧。</p>

        <p v-if="error" class="inline-error"><AlertCircle :size="16" />{{ error }}</p>
        <p v-if="success" class="inline-success"><CheckCircle2 :size="16" />{{ success }}</p>

        <div class="image-studio-actions">
          <button class="button button--primary" :disabled="pending" type="button" @click="generateImage">
            <LoaderCircle v-if="pending" class="button-icon-spin" :size="17" />
            <Sparkles v-else :size="17" />
            {{ pending ? '正在生成' : '生成图片' }}
          </button>
          <button v-if="pending" class="button button--secondary" type="button" @click="cancelGeneration">
            <X :size="16" />取消
          </button>
        </div>
      </section>

      <section class="image-studio-preview" aria-labelledby="image-preview-title">
        <div class="section-heading">
          <div>
            <p class="section-label">输出预览</p>
            <h3 id="image-preview-title">生成结果</h3>
          </div>
          <button v-if="generated && !pending" class="icon-button" title="清除结果" aria-label="清除结果" type="button" @click="clearResult">
            <X :size="17" />
          </button>
        </div>

        <div v-if="pending" class="image-preview-empty image-preview-empty--loading">
          <LoaderCircle class="button-icon-spin" :size="31" />
          <strong>正在等待真实图片</strong>
        </div>
        <div v-else-if="generated" class="image-result">
          <div class="image-result-frame">
            <img :src="generated.source" alt="AI 生成的商务视觉素材" />
          </div>
          <div class="image-result-meta">
            <div><span>画幅</span><strong>{{ generatedSizeLabel }}</strong></div>
            <div><span>格式</span><strong>{{ generated.response.content_type }}</strong></div>
            <div><span>大小</span><strong>{{ formatBytes(generated.response.byte_size) }}</strong></div>
          </div>
          <button class="button button--secondary image-download-button" type="button" @click="downloadImage">
            <Download :size="16" />下载图片
          </button>
        </div>
        <div v-else class="image-preview-empty">
          <ImageIcon :size="36" />
          <strong>暂无生成结果</strong>
        </div>
      </section>
    </div>
  </div>
</template>
