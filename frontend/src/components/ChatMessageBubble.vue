<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { CheckCheck, Clipboard, LoaderCircle, Volume2 } from 'lucide-vue-next'
import { difyApi, errorMessage } from '@/api/client'
import TraceTimeline from '@/components/TraceTimeline.vue'
import { useSpeech } from '@/composables/useSpeech'
import type { Artifact, ChatMessage } from '@/types'

interface ArtifactState {
  artifact: Artifact
  source: string
  loading: boolean
  error: string
}

const props = defineProps<{ message: ChatMessage }>()
const audioSource = ref('')
const audioContentType = ref('audio/wav')
const voiceLoading = ref(false)
const voicePlaying = ref(false)
const voiceError = ref('')
const browserFallbackAvailable = ref(false)
const artifactStates = ref<ArtifactState[]>([])
const { speak } = useSpeech()
let audio: HTMLAudioElement | undefined
let audioObjectUrl = ''
let artifactLoadGeneration = 0
const artifactObjectUrls = new Set<string>()

function roleLabel(role: ChatMessage['role']) {
  if (role === 'user' || role === 'enterprise_user') return '企业用户'
  if (role === 'support_agent' || role === 'agent') return '客服'
  if (role === 'executive' || role === 'manager') return '经营管理者'
  if (role === 'system') return '系统'
  return 'AI'
}

function roleAvatar(role: ChatMessage['role']) {
  if (role === 'user' || role === 'enterprise_user') return '企'
  if (role === 'support_agent' || role === 'agent') return '客'
  if (role === 'executive' || role === 'manager') return '管'
  if (role === 'system') return '系'
  return 'AI'
}

function stopAudio() {
  if (!audio) return
  audio.pause()
  audio.currentTime = 0
  voicePlaying.value = false
}

function releaseAudioObjectUrl() {
  if (!audioObjectUrl) return
  URL.revokeObjectURL(audioObjectUrl)
  audioObjectUrl = ''
}

function releaseArtifactObjectUrls() {
  for (const url of artifactObjectUrls) URL.revokeObjectURL(url)
  artifactObjectUrls.clear()
}

async function loadArtifacts(artifacts: Artifact[] | undefined) {
  const generation = ++artifactLoadGeneration
  releaseArtifactObjectUrls()
  artifactStates.value = (artifacts ?? []).map((artifact) => ({
    artifact,
    source: artifact.data_url || '',
    loading: Boolean(artifact.media_url) || (artifact.kind === 'image' && Boolean(artifact.data_url)),
    error: '',
  }))

  await Promise.all(artifactStates.value.map(async (state) => {
    if (!state.artifact.media_url || state.source) return
    try {
      const media = await difyApi.mediaProxy({
        url: state.artifact.media_url,
        kind: state.artifact.kind,
      })
      if (generation !== artifactLoadGeneration) return
      const source = URL.createObjectURL(media)
      artifactObjectUrls.add(source)
      state.source = source
      state.loading = state.artifact.kind === 'image'
    } catch (caught) {
      if (generation !== artifactLoadGeneration) return
      state.loading = false
      state.error = errorMessage(caught)
    }
  }))
}

function artifactLoaded(state: ArtifactState) {
  state.loading = false
}

function artifactFailed(state: ArtifactState) {
  state.loading = false
  state.error = state.artifact.kind === 'image' ? '生成的图片暂时无法显示' : '生成的音频暂时无法播放'
}

async function playVoice() {
  voiceError.value = ''
  browserFallbackAvailable.value = false
  if (voicePlaying.value) {
    stopAudio()
    return
  }
  if (audioSource.value && audio) {
    try {
      await audio.play()
      voicePlaying.value = true
    } catch (caught) {
      voiceError.value = errorMessage(caught)
    }
    return
  }
  voiceLoading.value = true
  try {
    const result = await difyApi.textToSpeech({ text: props.message.content, voice: 'Cherry' })
    let source = result.data_url || ''
    if (!source && result.media_url) {
      // Fetch through the authenticated backend proxy. This also repairs the
      // provider's non-canonical WAV length fields before Chrome decodes it.
      const media = await difyApi.mediaProxy({ url: result.media_url, kind: 'audio' })
      releaseAudioObjectUrl()
      source = URL.createObjectURL(media)
      audioObjectUrl = source
    }
    if (!source) throw new Error('TTS 未返回可播放的真实音频')
    audioSource.value = source
    audioContentType.value = result.content_type
    audio = new Audio(source)
    audio.preload = 'none'
    audio.onplay = () => { voicePlaying.value = true }
    audio.onpause = () => { voicePlaying.value = false }
    audio.onended = () => { voicePlaying.value = false }
    await audio.play()
  } catch (caught) {
    voiceError.value = errorMessage(caught)
    browserFallbackAvailable.value = true
  } finally {
    voiceLoading.value = false
  }
}

async function copyMessage() {
  await navigator.clipboard?.writeText(props.message.content)
}

onBeforeUnmount(() => {
  artifactLoadGeneration += 1
  stopAudio()
  releaseAudioObjectUrl()
  releaseArtifactObjectUrls()
})

watch(() => props.message.artifacts, loadArtifacts, { immediate: true, deep: true })
</script>

<template>
  <article class="chat-message" :class="`chat-message--${message.role}`">
    <div class="message-avatar">{{ roleAvatar(message.role) }}</div>
    <div class="message-main">
      <div class="message-meta">
        <strong>{{ roleLabel(message.role) }}</strong>
        <time v-if="message.created_at">{{ new Date(message.created_at).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) }}</time>
      </div>
      <div class="message-bubble" :class="{ 'message-bubble--error': message.error }">
        <span v-if="message.pending" class="typing-dots"><i /><i /><i /></span>
        <p v-else>{{ message.content }}</p>
      </div>
      <div v-if="artifactStates.length" class="message-artifacts">
        <div v-for="(state, index) in artifactStates" :key="`${state.artifact.kind}-${index}`" class="message-artifact">
          <div v-if="state.loading" class="message-artifact-loading"><LoaderCircle class="is-spinning" :size="17" />正在加载媒体</div>
          <img
            v-if="state.artifact.kind === 'image' && state.source"
            class="message-artifact-image"
            :class="{ 'message-artifact-image--loading': state.loading }"
            :src="state.source"
            alt="AI 生成图片"
            @load="artifactLoaded(state)"
            @error="artifactFailed(state)"
          />
          <audio
            v-else-if="state.artifact.kind === 'audio' && state.source"
            class="message-artifact-audio"
            :src="state.source"
            controls
            preload="metadata"
            aria-label="AI 生成语音"
            @loadedmetadata="artifactLoaded(state)"
            @error="artifactFailed(state)"
          />
          <p v-if="state.error" class="message-artifact-error" role="alert">{{ state.error }}</p>
        </div>
      </div>
      <div v-if="message.citations?.length" class="citation-list">
        <p class="section-label">知识依据</p>
        <article v-for="citation in message.citations" :key="citation.document_id" class="citation-item">
          <strong>{{ citation.title }}</strong>
          <span>{{ citation.excerpt }}</span>
          <small>匹配度 {{ Math.round(citation.score * 100) }}%</small>
        </article>
      </div>
      <details v-if="message.trace?.length" class="trace-details">
        <summary>查看 AI 决策过程（{{ message.trace.length }} 步）<CheckCheck :size="15" /></summary>
        <TraceTimeline :trace="message.trace" :pending="message.pending" />
      </details>
      <div v-if="message.role === 'assistant' && !message.pending" class="message-tools">
        <button class="icon-button" :class="{ 'icon-button--active': voicePlaying }" :disabled="voiceLoading" :title="voicePlaying ? '停止语音' : '播放真实 TTS 语音'" :aria-label="voicePlaying ? '停止语音' : '播放真实 TTS 语音'" @click="playVoice">
          <LoaderCircle v-if="voiceLoading" class="is-spinning" :size="16" /><Volume2 v-else :size="16" />
        </button>
        <button class="icon-button" title="复制回答" aria-label="复制回答" @click="copyMessage"><Clipboard :size="16" /></button>
      </div>
      <audio v-if="audioSource" class="message-audio" :src="audioSource" :type="audioContentType" controls preload="none" aria-label="真实 TTS 音频" @play="voicePlaying = true" @pause="voicePlaying = false" @ended="voicePlaying = false" />
      <p v-if="voiceError" class="message-voice-error" role="alert">{{ voiceError }}</p>
      <button v-if="browserFallbackAvailable" class="button button--quiet message-browser-voice" type="button" @click="speak(message.content)"><Volume2 :size="14" />使用浏览器朗读</button>
    </div>
  </article>
</template>
