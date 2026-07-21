import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import type { UserRole } from '@/types'

declare module 'vue-router' {
  interface RouteMeta {
    requiresAuth?: boolean
    roles?: UserRole[]
    title?: string
  }
}

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { title: '登录' } },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
      meta: { requiresAuth: true, roles: ['enterprise_user'], title: 'AI 智能对话助手' },
    },
    {
      path: '/tickets',
      name: 'tickets',
      component: () => import('@/views/TicketsView.vue'),
      meta: { requiresAuth: true, roles: ['support_agent'], title: '智能客服辅助' },
    },
    {
      path: '/ticket-request',
      name: 'ticket-request',
      redirect: { name: 'chat' },
      meta: { requiresAuth: true, roles: ['enterprise_user'], title: 'AI 智能对话助手' },
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeView.vue'),
        // Support staff maintain FAQ knowledge from the single customer-service
        // workspace; the standalone management route is administrator-only.
        meta: { requiresAuth: true, roles: ['admin'], title: '知识库管理' },
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('@/views/DashboardView.vue'),
      meta: { requiresAuth: true, roles: ['executive'], title: '经营决策大屏' },
    },
    {
      path: '/admin',
      name: 'admin',
      component: () => import('@/views/AdminView.vue'),
      meta: { requiresAuth: true, roles: ['admin'], title: '系统管理' },
    },
    {
      path: '/image-studio',
      name: 'image-studio',
      component: () => import('@/views/TextToImageView.vue'),
      meta: { requiresAuth: true, roles: ['support_agent', 'admin', 'executive'], title: '文字转图片' },
    },
    { path: '/:pathMatch(.*)*', name: 'not-found', component: () => import('@/views/NotFoundView.vue') },
  ],
})

export function homeForRole(role: UserRole | null | undefined) {
  if (role === 'support_agent') return { name: 'tickets' }
  if (role === 'admin') return { name: 'admin' }
  if (role === 'executive') return { name: 'dashboard' }
  return { name: 'chat' }
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.ready) await auth.initialize()
  if (to.meta.requiresAuth && !auth.isAuthenticated) return { name: 'login', query: { redirect: to.fullPath } }
  // Keep a role-protected deep link intact while the background profile
  // restore is still pending. Once the profile resolves, main.ts reruns the
  // guard for the active route and applies the final role decision.
  if (to.meta.roles?.length && auth.profileResolved && !auth.canAccess(to.meta.roles)) return homeForRole(auth.role)
  if (to.name === 'login' && auth.isAuthenticated && auth.profileResolved) return homeForRole(auth.role)
  return true
})

export default router
