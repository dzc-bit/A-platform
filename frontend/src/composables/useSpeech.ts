import { computed, ref } from 'vue'

interface SpeechResultEvent extends Event {
  results: ArrayLike<ArrayLike<{ transcript: string }>>
}

interface SpeechRecognitionInstance {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((event: SpeechResultEvent) => void) | null
  onerror: ((event: Event & { error?: string }) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}

interface SpeechRecognitionConstructor {
  new (): SpeechRecognitionInstance
}

declare global {
  interface Window {
    SpeechRecognition?: SpeechRecognitionConstructor
    webkitSpeechRecognition?: SpeechRecognitionConstructor
  }
}

export function useSpeech() {
  const listening = ref(false)
  const supported = computed(() => typeof window !== 'undefined' && Boolean(window.SpeechRecognition || window.webkitSpeechRecognition))
  let recognition: SpeechRecognitionInstance | undefined

  function listen(
    onResult: (text: string) => void,
    onError: (message: string) => void,
    language = 'zh-CN',
  ) {
    if (!supported.value) {
      onError('当前浏览器不支持语音输入，请使用文本输入。')
      return
    }
    const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition
    if (!Recognition) return
    recognition = new Recognition()
    recognition.lang = language
    recognition.interimResults = false
    recognition.continuous = false
    recognition.onresult = (event) => onResult(event.results[0]?.[0]?.transcript.trim() ?? '')
    recognition.onerror = (event) => onError(event.error === 'not-allowed' ? '未获得麦克风权限。' : '语音识别未能完成，请重试。')
    recognition.onend = () => { listening.value = false }
    listening.value = true
    recognition.start()
  }

  function stop() {
    recognition?.stop()
  }

  function speak(text: string, language = 'zh-CN') {
    if (!('speechSynthesis' in window)) return false
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(text)
    utterance.lang = language
    utterance.rate = 1
    window.speechSynthesis.speak(utterance)
    return true
  }

  return { listening, supported, listen, stop, speak }
}
