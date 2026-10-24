import path from 'node:path'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'

// The libraries go into chunks of their own, so no chunk passes Vite's 500 kB warning and a
// release that changes only the app's code leaves them cached in the browser.
const vendorChunks = {
  vue: /\/node_modules\/(vue|@vue|vue-router|pinia|@vueuse)\//,
  charts: /\/node_modules\/(@unovis|d3-[^/]+|internmap|@juggle)\//,
}

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          for (const [name, pattern] of Object.entries(vendorChunks)) {
            if (pattern.test(id)) return name
          }
          if (id.includes('/node_modules/')) return 'vendor'
        },
      },
    },
  },
  server: {
    allowedHosts: true,
  },
})
