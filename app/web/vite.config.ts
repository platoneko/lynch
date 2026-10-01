import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

// Dev server proxies /api and the pairing page to the running reader server (just app-start); without /pair,
// a 401's redirect there would get vite's SPA fallback and reload forever.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '100.93.210.104',
    port: 5173,
    proxy: { '/api': 'http://100.93.210.104:3151', '/pair': 'http://100.93.210.104:3151' },
  },
  test: { environment: 'node' },
});
