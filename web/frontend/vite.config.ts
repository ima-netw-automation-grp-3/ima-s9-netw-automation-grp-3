import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// En développement (npm run dev), l'API FastAPI tourne sur :8080.
const backend = 'http://localhost:8080';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      // Host d'origine conservé : l'API vérifie que l'en-tête Origin des POST désigne le même hôte.
      '/api': { target: backend, changeOrigin: false },
      '/ws': { target: backend.replace('http', 'ws'), ws: true },
      '/wireshark': { target: backend, changeOrigin: false, ws: true },
    },
  },
  build: { outDir: 'dist', chunkSizeWarningLimit: 2500 },
});
