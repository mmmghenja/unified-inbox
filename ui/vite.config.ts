import { fileURLToPath, URL } from 'node:url';

import vue from '@vitejs/plugin-vue';
import { defineConfig } from 'vite';

// Раздачу собранного `dist/` делает ядро (кусок 6a), не dev-сервер Vite — см. `docs/architecture.md`,
// раздел «Сборка и раздача». Поэтому здесь нет `server`/`dev`: только `build`/`build --watch`.
export default defineConfig({
  plugins: [vue()],
  base: '/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    assetsInlineLimit: 0, // ничего не инлайнить в index.html (CSP: ни одного инлайн-<script>/<style>)
    sourcemap: false,
    modulePreload: {
      polyfill: false, // полифил vite вставляется инлайн-скриптом в index.html — CSP script-src 'self' его не пустит
    },
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name].[hash].js',
        chunkFileNames: 'assets/[name].[hash].js',
        assetFileNames: 'assets/[name].[hash][extname]',
      },
    },
  },
});
