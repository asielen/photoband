import { svelte } from '@sveltejs/vite-plugin-svelte'
import { defineConfig } from 'vite'

// The UI is served by the Python backend; `npm run dev` proxies /api to a running
// `photoband serve --port 8765` for development.
export default defineConfig({
  plugins: [svelte()],
  base: '/',
  build: { target: ['chrome100', 'safari15', 'edge100'], sourcemap: false, chunkSizeWarningLimit: 1200 },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/fonts': 'http://127.0.0.1:8765',
    },
  },
  test: { environment: 'node' },
} as any)
