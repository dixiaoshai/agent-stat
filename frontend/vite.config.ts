import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: { outDir: '../agent_stats/static', emptyOutDir: true },
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        configure(proxy) {
          proxy.on('proxyReq', (outgoing, incoming) => {
            // Preserve the backend's origin boundary; adapt only this local dev server.
            if (incoming.headers.origin === 'http://127.0.0.1:5173') {
              outgoing.setHeader('Origin', 'http://127.0.0.1:8765');
            }
          });
        },
      },
    },
  },
});
