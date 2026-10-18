import react from '@vitejs/plugin-react';
import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

// The offline copy of the app (src/offline/sync.ts) needs every hashed file of this build, KaTeX fonts included.
const precacheList: Plugin = {
  name: 'lynch-precache',
  generateBundle(_, bundle) {
    const assets = Object.keys(bundle).filter((f) => f.startsWith('assets/')).map((f) => `/${f}`);
    this.emitFile({ type: 'asset', fileName: 'precache.json', source: JSON.stringify(assets) });
  },
};

// Dev server proxies /api and the pairing page to the running reader server (just app-start); without /pair,
// a 401's redirect there would get vite's SPA fallback and reload forever.
export default defineConfig({
  plugins: [react(), precacheList],
  server: {
    host: '100.93.210.104',
    port: 5173,
    proxy: { '/api': 'http://100.93.210.104:3151', '/pair': 'http://100.93.210.104:3151' },
  },
  test: { environment: 'node' },
});
