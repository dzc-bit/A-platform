<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Bot, Eye, EyeOff, LockKeyhole, LogIn, Mail, ShieldCheck } from 'lucide-vue-next'
import { errorMessage } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()
const mode = ref<'login' | 'register'>('login')
const selectedDemo = ref('enterprise')
const email = ref('enterprise@neusoft.local')
const password = ref('Demo123!')
const displayName = ref('')
const pending = ref(false)
const error = ref('')
const showPassword = ref(false)

const demos = {
  enterprise: { label: '企业用户', email: 'enterprise@neusoft.local', hint: '咨询业务规则、检索知识依据' },
  support: { label: '客服专员', email: 'support@neusoft.local', hint: '处理工单并确认最终回复' },
  admin: { label: '系统管理员', email: 'admin@neusoft.local', hint: '管理用户、知识和 AI 配置' },
  executive: { label: '经营管理者', email: 'executive@neusoft.local', hint: '查看运营分析与服务概览' },
} as const

const demoHint = computed(() => demos[selectedDemo.value as keyof typeof demos].hint)

function chooseDemo() {
  email.value = demos[selectedDemo.value as keyof typeof demos].email
  password.value = 'Demo123!'
  error.value = ''
}

function toggleMode() {
  mode.value = mode.value === 'login' ? 'register' : 'login'
  error.value = ''
  if (mode.value === 'login') chooseDemo()
  else {
    displayName.value = ''
    email.value = ''
    password.value = ''
  }
}

async function submit() {
  pending.value = true
  error.value = ''
  try {
    if (mode.value === 'login') await auth.login(email.value.trim(), password.value)
    else await auth.register(email.value.trim(), password.value, displayName.value.trim())
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/chat'
    await router.replace(redirect)
  } catch (caught) {
    error.value = errorMessage(caught)
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <div class="login-shell">
      <header class="login-hero">
        <span class="brand-mark brand-mark--hero"><Bot :size="39" /></span>
        <h1>东软智慧商务</h1>
        <p>AI 助手平台 · Enterprise AI Service Workspace</p>
      </header>

      <section class="login-card">
        <form class="login-form" @submit.prevent="submit">
          <div class="form-heading">
            <p class="eyebrow">{{ mode === 'login' ? '账户登录' : '企业用户注册' }}</p>
            <h2>{{ mode === 'login' ? '欢迎回来' : '创建企业账户' }}</h2>
            <p>{{ mode === 'login' ? '登录您的账户以继续' : '注册后将以企业用户身份进入工作台' }}</p>
          </div>

          <div v-if="mode === 'login'" class="login-demo-field">
            <label class="field-label" for="demo">登录身份</label>
            <select id="demo" v-model="selectedDemo" class="field-control" @change="chooseDemo">
              <option v-for="(demo, key) in demos" :key="key" :value="key">{{ demo.label }}</option>
            </select>
            <p class="field-hint">{{ demoHint }}</p>
          </div>

          <label v-if="mode === 'register'" class="field-label" for="display-name">显示名称</label>
          <div v-if="mode === 'register'" class="login-input-wrap">
            <Bot :size="19" aria-hidden="true" />
            <input id="display-name" v-model="displayName" class="login-input" autocomplete="name" minlength="2" maxlength="80" required placeholder="请输入显示名称" />
          </div>

          <label class="field-label" for="email">邮箱</label>
          <div class="login-input-wrap">
            <Mail :size="19" aria-hidden="true" />
            <input id="email" v-model="email" class="login-input" type="email" autocomplete="username" required placeholder="请输入邮箱" />
          </div>

          <label class="field-label" for="password">密码</label>
          <div class="login-input-wrap login-input-wrap--password">
            <LockKeyhole :size="19" aria-hidden="true" />
            <input id="password" v-model="password" class="login-input" :type="showPassword ? 'text' : 'password'" :autocomplete="mode === 'login' ? 'current-password' : 'new-password'" minlength="8" required placeholder="请输入密码" />
            <button class="icon-button password-toggle" type="button" :title="showPassword ? '隐藏密码' : '显示密码'" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword">
              <EyeOff v-if="showPassword" :size="19" /><Eye v-else :size="19" />
            </button>
          </div>

          <p v-if="error" class="form-error" role="alert">{{ error }}</p>
          <button class="button button--primary button--wide login-submit" :disabled="pending" type="submit">
            <span v-if="pending" class="button-spinner" />
            <LogIn v-else :size="19" />
            {{ pending ? '正在验证' : mode === 'login' ? '登录' : '注册并进入工作台' }}
          </button>
        </form>
      </section>

      <button class="button button--quiet auth-mode-toggle" type="button" :disabled="pending" @click="toggleMode">
        {{ mode === 'login' ? '还没有账户？ 注册企业用户' : '已有账户？ 返回登录' }}
      </button>

      <footer class="login-footer">
        <span><ShieldCheck :size="16" />本地演示环境已启用，账号数据可直接体验。</span>
        <span>© 2026 东软智慧商务 · All rights reserved.</span>
      </footer>
    </div>
  </main>
</template>
