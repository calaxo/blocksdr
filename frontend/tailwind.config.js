/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#080d18',
        card: '#0f1626',
        card2: '#0b1220',
        ink: '#e6edf7',
        mut: '#8195b4',
        line: '#1e2b42',
        accent: '#38bdf8',
        good: '#37d67a',
        warn: '#f6c945',
        bad: '#ff6b6b',
        // Une couleur par catégorie de bloc : on reconnaît le rôle d'un bloc
        // à sa teinte avant même de lire son nom.
        source: '#f59e0b',
        proc: '#8b5cf6',
        xform: '#38bdf8',
        spectral: '#10b981',
        sink: '#f43f5e',
      },
      boxShadow: {
        card: '0 1px 0 0 rgba(255,255,255,.03) inset, 0 10px 30px -18px rgba(0,0,0,.8)',
      },
      fontFamily: {
        sans: ['system-ui', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'Consolas', 'monospace'],
      },
    },
  },
  plugins: [],
}
