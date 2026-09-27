import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    watch: { usePolling: process.env.CHOKIDAR_USEPOLLING === 'true' },
    proxy: {
      '/api': { target: process.env.DEV_API_URL || 'http://localhost:9079', changeOrigin: true },
      '/ws': { target: process.env.DEV_API_URL || 'http://localhost:9079', ws: true },
    },
  },
})
