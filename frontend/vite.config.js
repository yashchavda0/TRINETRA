import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The API base URL the console talks to. Override with VITE_API_TARGET when the
// registry service is not on this machine.
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    // GISMap uses relative URLs, so without these proxies every call would hit
    // the dev server's own origin and 404.
    proxy: {
      '/api/v1': { target: API_TARGET, changeOrigin: true },
      '/api/v2': { target: API_TARGET, changeOrigin: true },
      // The P0 alert stream. ws:true is required or Vite answers the upgrade
      // request with HTML and the socket loops on reconnect.
      '/alerts': { target: API_TARGET, changeOrigin: true, ws: true },
      '/health': { target: API_TARGET, changeOrigin: true },
    },
  },
  preview: { port: 4173, strictPort: true },
  build: { outDir: 'dist', sourcemap: true },
});
