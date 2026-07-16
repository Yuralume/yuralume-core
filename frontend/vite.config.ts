import { defineConfig, loadEnv, type Plugin } from 'vite'
import vue from '@vitejs/plugin-vue'
import { VitePWA } from 'vite-plugin-pwa'
import { resolve } from 'path'

/** Attach error handlers to every incoming socket so ECONNRESET won't crash the dev server. */
function socketErrorGuard(): Plugin {
  return {
    name: 'socket-error-guard',
    configureServer(server) {
      server.httpServer?.on('connection', (socket) => {
        socket.on('error', () => {})
      })
    },
  }
}

function parsePort(value: string | undefined, fallback: number): number {
  const parsed = Number.parseInt(value || '', 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

/** Parse comma-separated host list. Supports the special value "all" to disable host check. */
function parseAllowedHosts(value: string | undefined): true | string[] | undefined {
  if (!value) return undefined
  const trimmed = value.trim()
  if (!trimmed) return undefined
  if (trimmed === 'all' || trimmed === '*') return true
  return trimmed
    .split(',')
    .map((h) => h.trim())
    .filter(Boolean)
}

export default defineConfig(({ mode }) => {
  const repoRoot = resolve(__dirname, '..')
  const env = loadEnv(mode, repoRoot, '')
  const backendHost = env.VITE_BACKEND_HOST || '127.0.0.1'
  const backendPort = parsePort(env.VITE_BACKEND_PORT, 8002)
  const frontendHost = env.VITE_FRONTEND_HOST || '0.0.0.0'
  const frontendPort = parsePort(env.VITE_FRONTEND_PORT, 5174)
  const allowedHosts = parseAllowedHosts(env.VITE_ALLOWED_HOSTS)

  return {
    envDir: repoRoot,
    plugins: [
      vue(),
      VitePWA({
        strategies: 'injectManifest',
        srcDir: 'src',
        filename: 'sw.ts',
        registerType: 'autoUpdate',
        injectRegister: 'auto',
        manifest: {
          name: 'Yuralume',
          short_name: 'Yuralume',
          description: 'AI companion stage and LumeGram notifications',
          start_url: '/',
          scope: '/',
          display: 'standalone',
          background_color: '#100f14',
          theme_color: '#b75d3f',
          icons: [
            {
              src: '/favicon.png',
              sizes: '192x192',
              type: 'image/png',
            },
            {
              src: '/logo-mark.png',
              sizes: '512x512',
              type: 'image/png',
              purpose: 'any maskable',
            },
          ],
        },
      }),
      socketErrorGuard(),
    ],
    resolve: {
      alias: {
        '@': resolve(__dirname, 'src'),
      },
    },
    server: {
      host: frontendHost,
      port: frontendPort,
      ...(allowedHosts !== undefined ? { allowedHosts } : {}),
      fs: {
        allow: ['..'],
      },
      proxy: {
        '/api': {
          target: `http://${backendHost}:${backendPort}`,
          changeOrigin: true,
          ws: true,
        },
        '/health': {
          target: `http://${backendHost}:${backendPort}`,
          changeOrigin: true,
        },
        '/uploads': {
          target: `http://${backendHost}:${backendPort}`,
          changeOrigin: true,
        },
        '/v1/public': {
          target: `http://${backendHost}:${backendPort}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: '../src/kokoro_link/frontend/dist',
      emptyOutDir: true,
    },
  }
})
