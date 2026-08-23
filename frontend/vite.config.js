import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import Pages from 'vite-plugin-pages'
import path from 'node:path'

// Front BlockSDR : React + Tailwind + Vite. Mêmes conventions que surfcast.
// - vite-plugin-pages : routing basé sur les fichiers de src/pages
// - alias '@' -> src
// - proxy /api ET /ws vers le backend Python, pour éviter tout souci de CORS
//   en développement et garder la même URL qu'en production.
//   Le proxy WebSocket exige ws: true, sinon la connexion est refusée en 400.
export default defineConfig({
  plugins: [
    react(),
    Pages({ dirs: 'src/pages', extensions: ['jsx'] }),
  ],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    host: true,          // écoute sur 0.0.0.0 : indispensable depuis un conteneur
    // ⚠️ PAS 5173 : surfcast est une PWA et son service worker reste enregistré
    // sur cette origine. Il sert son propre cache et on croit que BlockSDR ne
    // démarre pas — alors qu-on regarde surfcast. Un port à nous règle le
    // problème définitivement.
    port: 5180,
    proxy: {
      '/api': { target: process.env.API_TARGET || 'http://localhost:8000', changeOrigin: true },
      '/ws': { target: process.env.API_TARGET || 'http://localhost:8000', ws: true },
    },
  },
})
