import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 产物交给 admin.py 现读现发：dist/index.html 不缓存，dist/assets/* 按 hash 长期缓存
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  // 开发期只起前端，/api 转发给 python admin.py（panel 的 8099）
  server: { proxy: { '/api': 'http://127.0.0.1:8099' } },
});
