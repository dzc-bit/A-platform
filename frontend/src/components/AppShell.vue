<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  BarChart3,
  Bot,
  ImagePlus,
  LogOut,
  Menu,
  MessageSquareText,
  Settings2,
  X,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth'
import { homeForRole } from '@/router'
import type { UserRole } from '@/types'

interface NavigationItem {
  label: string
  to: string
  icon: typeof MessageSquareText
  roles?: UserRole[]
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const sidebarOpen = ref(false)

const items: NavigationItem[] = [
  { label: 'AI 智能对话助手', to: '/chat', icon: MessageSquareText, roles: ['enterprise_user'] },
  { label: '智能客服辅助', to: '/tickets', icon: MessageSquareText, roles: ['support_agent'] },
  { label: '系统管理', to: '/admin', icon: Settings2, roles: ['admin'] },
  { label: '经营决策大屏', to: '/dashboard', icon: BarChart3, roles: ['executive'] },
  { label: '文字转图片', to: '/image-studio', icon: ImagePlus, roles: ['support_agent', 'admin', 'executive'] },
]

const visibleItems = computed(() => items.filter((item) => auth.canAccess(item.roles)))
const homePath = computed(() => homeForRole(auth.role).name === 'tickets'
  ? '/tickets'
  : homeForRole(auth.role).name === 'admin'
    ? '/admin'
    : homeForRole(auth.role).name === 'dashboard'
      ? '/dashboard'
      : '/chat')
const roleName = computed(() => ({
  enterprise_user: '企业用户',
  support_agent: '客服专员',
  admin: '系统管理员',
  executive: '经营管理者',
}[auth.user?.role ?? 'enterprise_user']))

function navigate() {
  sidebarOpen.value = false
}

function logout() {
  auth.logout()
  sidebarOpen.value = false
  router.push('/login')
}
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ 'sidebar--open': sidebarOpen }" aria-label="主导航">
      <div class="brand-row">
        <RouterLink class="brand" :to="homePath" @click="navigate">
          <span class="brand-mark"><Bot :size="22" /></span>
          <span class="brand-copy"><strong>东软智慧商务</strong><small>AI 助手平台</small></span>
        </RouterLink>
        <button class="icon-button sidebar-close" title="收起导航" aria-label="收起导航" @click="sidebarOpen = false">
          <X :size="19" />
        </button>
      </div>

      <nav class="primary-nav">
        <RouterLink
          v-for="item in visibleItems"
          :key="item.to"
          :to="item.to"
          class="nav-item"
          :class="{ 'nav-item--active': route.path === item.to }"
          @click="navigate"
        >
          <component :is="item.icon" :size="19" />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <div class="sidebar-footer">
        <div class="user-identity">
          <span class="avatar">{{ auth.user?.display_name?.slice(0, 1) || '用' }}</span>
          <span class="user-copy"><strong>{{ auth.user?.display_name || '正在载入' }}</strong><small>{{ roleName }}</small></span>
        </div>
        <button class="icon-button logout-button" title="退出登录" aria-label="退出登录" @click="logout">
          <LogOut :size="18" />
        </button>
      </div>
    </aside>

    <button
      class="sidebar-scrim"
      :class="{ 'sidebar-scrim--visible': sidebarOpen }"
      aria-label="关闭导航"
      @click="sidebarOpen = false"
    />

    <section class="workspace">
      <header class="topbar">
        <button class="icon-button mobile-menu" title="打开导航" aria-label="打开导航" @click="sidebarOpen = true">
          <Menu :size="21" />
        </button>
        <div>
          <p class="eyebrow">业务协同工作台</p>
          <h1>{{ route.meta.title }}</h1>
        </div>
        <div class="topbar-status"><span class="status-dot" />服务运行正常</div>
      </header>
      <main class="page-content"><slot /></main>
    </section>
  </div>
</template>
