import { defineStore } from 'pinia'
import axios from 'axios'
import { authApi, TOKEN_KEY } from '@/api/client'
import type { User, UserRole } from '@/types'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as User | null,
    ready: false,
    hasSession: Boolean(localStorage.getItem(TOKEN_KEY)),
    profileResolved: false,
    bootstrapError: '' as string,
    retryAttempt: 0,
    retryTimer: undefined as ReturnType<typeof window.setTimeout> | undefined,
  }),
  getters: {
    isAuthenticated: (state) => state.hasSession,
    role: (state): UserRole | null => state.user?.role ?? null,
  },
  actions: {
    clearSessionRetry() {
      if (this.retryTimer !== undefined) {
        window.clearTimeout(this.retryTimer)
        this.retryTimer = undefined
      }
      this.retryAttempt = 0
    },
    scheduleSessionRetry() {
      if (this.retryTimer !== undefined || !this.hasSession) return
      const delay = Math.min(30_000, 1_000 * (2 ** Math.min(this.retryAttempt, 5)))
      this.retryAttempt += 1
      this.retryTimer = window.setTimeout(() => {
        this.retryTimer = undefined
        void this.initialize(true)
      }, delay)
    },
    async initialize(force = false) {
      if (this.ready && !force) return
      this.bootstrapError = ''
      this.hasSession = Boolean(localStorage.getItem(TOKEN_KEY))
      if (!this.hasSession) {
        this.user = null
        this.profileResolved = true
        this.ready = true
        this.clearSessionRetry()
        return
      }

      // A stored token is enough to enter a protected route while /auth/me
      // restores profile details in the background. This avoids a cold backend
      // leaving the application on its static startup screen.
      this.ready = true
      this.profileResolved = false
      try {
        this.user = await authApi.me()
        this.profileResolved = true
        this.clearSessionRetry()
      } catch (error) {
        const status = axios.isAxiosError(error) ? error.response?.status : undefined
        if (status === 401 || status === 403) {
          localStorage.removeItem(TOKEN_KEY)
          this.user = null
          this.hasSession = false
          this.profileResolved = true
          this.clearSessionRetry()
        } else {
          // A cold backend or proxy must not destroy an otherwise valid session.
          this.bootstrapError = '暂时无法验证登录状态，服务恢复后会自动重试。'
          this.scheduleSessionRetry()
        }
      }
    },
    async login(email: string, password: string) {
      const result = await authApi.login({ email, password })
      this.applyAuthentication(result)
      return result.user
    },
    async register(email: string, password: string, displayName: string) {
      const result = await authApi.register({ email, password, display_name: displayName })
      this.applyAuthentication(result)
      return result.user
    },
    applyAuthentication(result: Awaited<ReturnType<typeof authApi.login>>) {
      localStorage.setItem(TOKEN_KEY, result.access_token)
      this.user = result.user
      this.ready = true
      this.hasSession = true
      this.profileResolved = true
      this.clearSessionRetry()
      this.bootstrapError = ''
    },
    logout() {
      localStorage.removeItem(TOKEN_KEY)
      this.user = null
      this.hasSession = false
      this.profileResolved = true
      this.clearSessionRetry()
      this.bootstrapError = ''
    },
    canAccess(roles?: UserRole[]) {
      return !roles?.length || Boolean(this.user && roles.includes(this.user.role))
    },
  },
})
