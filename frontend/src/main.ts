import { createApp } from 'vue'
import axios from 'axios'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import './style.css'
import './styles/brand-effects.css'
import App from './App.vue'
import router from './router'
import { i18n } from '@/i18n'
import { clearStoredToken, getStoredToken, useAuth } from '@/composables/useAuth'
import { registerServiceWorker } from '@/utils/registerServiceWorker'
import { bounceToSignIn } from '@/utils/sessionBounce'

// Global axios interceptors — auth header + 401 handling.
//
// Every API client under src/utils/api/*.ts uses the bare `axios`
// import, so attaching here propagates to all of them. We don't
// branch on KOKORO_AUTH_ENABLED here — that's a backend concern.
// If a token is present we always send it; if auth is disabled the
// backend ignores it.
axios.interceptors.request.use((config) => {
  const token = getStoredToken()
  if (token) {
    config.headers.set('Authorization', `Bearer ${token}`)
  }
  return config
})

axios.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      // Hosted players get the Portal exit, self-host gets /login — see
      // `@/utils/sessionRedirect`. Screens that are themselves part of
      // signing in are skipped so the form can show its own error.
      bounceToSignIn({
        router,
        clearToken: clearStoredToken,
        portalUrl: useAuth().portalUrl.value,
      })
    }
    return Promise.reject(error)
  },
)

const app = createApp(App)
app.use(Antd)
app.use(i18n)
app.use(router)
app.mount('#app')

registerServiceWorker()
