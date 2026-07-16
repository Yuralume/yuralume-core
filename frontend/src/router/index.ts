import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useAuth } from '@/composables/useAuth'

// Phase 3 結束後 admin 子頁全部接到真正的 page 元件；AdminPlaceholder 留著
// 給未來新增 admin 入口時當佔位用。每個 admin route 都是 lazy-loaded。
const adminPlaceholderRoutes: RouteRecordRaw[] = [
  {
    path: 'characters',
    name: 'admin-characters',
    component: () => import('@/pages/admin/CharactersAdminPage.vue'),
  },
  {
    path: 'memories',
    name: 'admin-memories',
    component: () => import('@/pages/admin/MemoriesAdminPage.vue'),
  },
  {
    path: 'channels',
    name: 'admin-channels',
    component: () => import('@/pages/admin/ChannelsAdminPage.vue'),
  },
  {
    path: 'dispositions',
    name: 'admin-dispositions',
    component: () => import('@/pages/admin/DispositionAdminPage.vue'),
    meta: { debugOnly: true },
  },
  {
    path: 'providers',
    name: 'admin-providers',
    component: () => import('@/pages/admin/ProviderSettingsAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'models',
    name: 'admin-models',
    component: () => import('@/pages/admin/ModelsAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'image-profiles',
    name: 'admin-image-profiles',
    component: () => import('@/pages/admin/ImageProfilesAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'video-profiles',
    name: 'admin-video-profiles',
    component: () => import('@/pages/admin/VideoProfilesAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'voice',
    name: 'admin-voice',
    component: () => import('@/pages/admin/VoiceAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'loras',
    name: 'admin-loras',
    component: () => import('@/pages/admin/LorasAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'proactive',
    name: 'admin-proactive',
    component: () => import('@/pages/admin/ProactiveAdminPage.vue'),
    meta: { debugOnly: true },
  },
  {
    path: 'schedule',
    name: 'admin-schedule',
    component: () => import('@/pages/admin/ScheduleAdminPage.vue'),
  },
  {
    path: 'follow-ups',
    name: 'admin-follow-ups',
    component: () => import('@/pages/admin/FollowUpsAdminPage.vue'),
    meta: { debugOnly: true },
  },
  {
    path: 'world',
    name: 'admin-world',
    component: () => import('@/pages/admin/WorldAdminPage.vue'),
  },
  {
    path: 'site-settings',
    name: 'admin-site-settings',
    component: () => import('@/pages/admin/SiteSettingsAdminPage.vue'),
    meta: { cloudLocked: true },
  },
  {
    path: 'character-freeze',
    name: 'admin-character-freeze',
    component: () => import('@/pages/admin/CharacterFreezeAdminPage.vue'),
  },
  {
    path: 'dev-docs',
    name: 'admin-dev-docs',
    component: () => import('@/pages/admin/DevDocsAdminPage.vue'),
  },
  {
    path: 'dev-docs/:slug',
    name: 'admin-dev-docs-detail',
    component: () => import('@/pages/admin/DevDocsAdminPage.vue'),
  },
  {
    path: 'observability',
    name: 'admin-observability',
    component: () => import('@/pages/admin/ObservabilityAdminPage.vue'),
  },
  {
    path: 'users',
    name: 'admin-users',
    component: () => import('@/pages/admin/UsersAdminPage.vue'),
    meta: { cloudLocked: true },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/pages/LoginPage.vue'),
      meta: { layout: 'auth', public: true },
    },
    {
      path: '/setup',
      name: 'setup',
      component: () => import('@/pages/SetupPage.vue'),
      meta: { layout: 'auth', public: true },
    },
    {
      path: '/demo/oauth/:provider/start',
      name: 'demo-oauth-start',
      component: () => import('@/pages/DemoOAuthStartPage.vue'),
      meta: { layout: 'auth', public: true },
    },
    {
      path: '/demo/oauth/:provider/callback',
      name: 'demo-oauth-callback',
      component: () => import('@/pages/DemoOAuthCallbackPage.vue'),
      meta: { layout: 'auth', public: true },
    },
    {
      path: '/cloud/callback',
      name: 'cloud-callback',
      component: () => import('@/pages/CloudCallbackPage.vue'),
      meta: { layout: 'auth', public: true },
    },
    {
      path: '/',
      name: 'stage',
      component: () => import('@/pages/StagePage.vue'),
      meta: { layout: 'player' },
    },
    {
      path: '/studio',
      name: 'studio',
      component: () => import('@/pages/StudioPage.vue'),
      meta: { layout: 'player' },
      redirect: { name: 'studio-authoring' },
      children: [
        {
          path: '',
          name: 'studio-authoring',
          component: () => import('@/pages/StudioAuthoringPage.vue'),
        },
        {
          path: 'fusion-stories',
          name: 'studio-fusion-stories',
          component: () => import('@/pages/FusionStoryPage.vue'),
        },
        {
          path: 'branching-dramas',
          name: 'studio-branching-dramas',
          component: () => import('@/pages/BranchingDramaPage.vue'),
        },
        {
          path: 'character-cards',
          name: 'studio-character-cards',
          component: () => import('@/pages/StudioCardsPage.vue'),
        },
      ],
    },
    {
      path: '/fusion-story',
      redirect: { name: 'studio-fusion-stories' },
    },
    {
      path: '/branching-drama',
      redirect: { name: 'studio-branching-dramas' },
    },
    {
      // Phase 2 預留入口：MemoirPage 真正內容於 Phase 4 補上
      path: '/memoir/:characterId?',
      name: 'memoir',
      component: () => import('@/pages/MemoirPage.vue'),
      meta: { layout: 'player' },
    },
    {
      // Dev-only style guide。Phase 1 ~ 5 重構期間用來回歸 UI primitives。
      // 完成全面遷移後可以移除此路由（或加 import.meta.env.DEV 守門）。
      path: '/_styleguide',
      name: 'styleguide',
      component: () => import('@/pages/StyleGuidePage.vue'),
      meta: { layout: 'player' },
    },
    {
      // Admin 區：AdminLayout 自帶左側 nav + 頂部 breadcrumb + 內部 <router-view />。
      // 子頁透過 nested routes 渲染進 AdminLayout 的 content slot。
      path: '/admin',
      component: () => import('@/layouts/AdminLayout.vue'),
      meta: { layout: 'admin', requiresAdmin: true },
      children: [
        {
          path: '',
          name: 'admin-home',
          component: () => import('@/pages/admin/AdminHomePage.vue'),
        },
        ...adminPlaceholderRoutes,
      ],
    },
  ],
})

// ----------------------------------------------------------------------
// Auth guard
// ----------------------------------------------------------------------
//
// On the first navigation the auth state hasn't been probed yet
// (bootstrapAuth runs against GET /auth/config). The guard awaits it
// so subsequent guards see the resolved authEnabled / needsSetup
// values without race conditions.
//
// Routing rules:
//   - Public routes (login / setup): always allowed; but bounce off
//     /setup if setup is already complete, off /login if no-auth mode.
//   - Disabled-auth mode: every other route allowed; landing on
//     /login or /setup redirects home.
//   - Enabled-auth mode + needs_setup: every other route → /setup.
//   - Enabled-auth mode + has token + currentUser: allowed.
//   - Enabled-auth mode + missing/invalid token: → /login?redirect=...

router.beforeEach(async (to) => {
  const auth = useAuth()
  if (!auth.authProbed.value) {
    await auth.bootstrapAuth()
  }

  const isPublic = Boolean(to.meta?.public)

  // Disabled mode: route freely; the login / setup screens are
  // dead ends so bounce home instead.
  if (!auth.authEnabled.value) {
    if (to.name === 'login' || to.name === 'setup') {
      return { path: '/' }
    }
    return true
  }

  // Enabled mode below.
  if (auth.needsSetup.value && to.name !== 'setup') {
    return { name: 'setup' }
  }
  if (!auth.needsSetup.value && to.name === 'setup') {
    return { name: 'login' }
  }

  if (isPublic) {
    // /login should bounce home if already authenticated.
    if (to.name === 'login' && auth.isAuthenticated.value) {
      return { path: '/' }
    }
    return true
  }

  if (!auth.isAuthenticated.value) {
    const here = to.fullPath
    return {
      name: 'login',
      query: here === '/' ? {} : { redirect: here },
    }
  }

  // Admin-only routes: when auth is enabled, gate behind is_admin so
  // non-admin users can't even see the admin shell — backend already
  // 403s every /admin/* endpoint, but routing them away avoids the
  // "page loads then errors" UX. In disabled-auth mode the single-
  // machine owner is implicitly admin, so this check is skipped.
  const requiresAdmin = to.matched.some(record => record.meta?.requiresAdmin)
  if (requiresAdmin && !auth.isAdmin.value) {
    return { path: '/' }
  }

  // Developer-only routes (observability, experiments, follow-ups,
  // disposition / pattern timelines, proactive funnel). Hidden from
  // both nav and direct URL access unless the deployment owner set
  // ``KOKORO_DEBUG_UI_ENABLED=true``. Backend admin APIs stay
  // reachable for curl-based exports regardless.
  const isDebugOnly = to.matched.some(record => record.meta?.debugOnly)
  if (isDebugOnly && !auth.debugUiEnabled.value) {
    return { name: 'admin-home' }
  }
  const isCloudLocked = to.matched.some(record => record.meta?.cloudLocked)
  if (isCloudLocked && auth.cloudMode.value) {
    return { name: 'admin-home' }
  }
  return true
})

export default router
