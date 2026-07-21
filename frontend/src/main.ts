import { createApp, watch } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { AUTH_SESSION_EXPIRED_EVENT } from './api/client'
import router, { homeForRole } from './router'
import { useAuthStore } from './stores/auth'
import './styles.css'

async function bootstrap() {
  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)
  app.use(router)

  // Route guards can use the locally stored token immediately. The profile is
  // restored in the background, so a cold backend does not require a refresh.
  const auth = useAuthStore(pinia)
  window.addEventListener(AUTH_SESSION_EXPIRED_EVENT, () => {
    const current = router.currentRoute.value
    const redirect = current.meta.requiresAuth ? current.fullPath : undefined
    auth.logout()
    if (current.name !== 'login') {
      void router.replace({ name: 'login', query: redirect ? { redirect } : undefined })
    }
  })
  void auth.initialize()

  watch(
    () => [router.currentRoute.value.fullPath, auth.isAuthenticated, auth.profileResolved, auth.role] as const,
    () => {
      const current = router.currentRoute.value
      if (current.meta.requiresAuth && !auth.isAuthenticated) {
        void router.replace({ name: 'login', query: { redirect: current.fullPath } })
      } else if (current.meta.roles?.length && auth.profileResolved && !auth.canAccess(current.meta.roles)) {
        void router.replace(homeForRole(auth.role))
      } else if (current.name === 'login' && auth.isAuthenticated && auth.profileResolved) {
        void router.replace(homeForRole(auth.role))
      }
    },
  )
  await router.isReady()
  app.mount('#app')
}

void bootstrap()
