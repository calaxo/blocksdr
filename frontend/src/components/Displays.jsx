import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useApp } from '@/store'
import { paletteOf } from '@/lib/palettes'

// Les afficheurs. Règle de fond : AUCUNE trame ne passe par l’état React.
// Chaque vue s-abonne au bus, garde la dernière trame dans une ref, et dessine
// dans son canvas. À 25 images par seconde et par afficheur, tout autre choix
// ferait ramer le navigateur pour rien.

// Prépare un canvas net sur écran haute densité et suit les changements de
// taille. Sans le facteur devicePixelRatio, tout est flou sur un portable.
function useCanvas(draw) {
  const ref = useRef(null)
  const size = useRef({ w: 0, h: 0 })

  useLayoutEffect(() => {
    const cv = ref.current
    if (!cv) return
    const fit = () => {
      const r = cv.getBoundingClientRect()
      const dpr = window.devicePixelRatio || 1
      const w = Math.max(1, Math.round(r.width * dpr))
      const h = Math.max(1, Math.round(r.height * dpr))
      if (w !== cv.width || h !== cv.height) {
        cv.width = w; cv.height = h
        size.current = { w, h }
        draw?.(cv, true)
      }
    }
    fit()
    const ro = new ResizeObserver(fit)
    ro.observe(cv)
    return () => ro.disconnect()
  }, [draw])

  return ref
}

const fmtHz = (hz) => {
  const a = Math.abs(hz)
  if (a >= 1e9) return `${(hz / 1e9).toFixed(3)} GHz`
  if (a >= 1e6) return `${(hz / 1e6).toFixed(3)} MHz`
  if (a >= 1e3) return `${(hz / 1e3).toFixed(1)} kHz`
  return `${hz.toFixed(0)} Hz`
}

function Frame({ title, subtitle, children, right }) {
  return (
    <div className="panel flex flex-col overflow-hidden min-w-[280px] flex-1">
      <div className="flex items-baseline gap-2 px-3 py-1.5 border-b border-line">
        <span className="text-[12px] font-semibold">{title}</span>
        <span className="text-[10px] text-mut font-mono truncate">{subtitle}</span>
        <span className="ml-auto text-[10px] text-mut">{right}</span>
      </div>
      <div className="relative flex-1 min-h-0">{children}</div>
    </div>
  )
}

// ---------------------------------------------------------------- spectre --
function SpectrumView({ id, label }) {
  const { bus } = useApp()
  const last = useRef(null)
  const [meta, setMeta] = useState(null)

  const draw = useRef(() => {})
  const ref = useCanvas((cv) => draw.current(cv))

  useEffect(() => {
    draw.current = (cv) => {
      const g = cv.getContext('2d')
      const { width: w, height: h } = cv
      g.clearRect(0, 0, w, h)
      g.fillStyle = '#0b1220'; g.fillRect(0, 0, w, h)

      // grille : 10 divisions horizontales, 4 verticales
      g.strokeStyle = '#16223a'; g.lineWidth = 1
      g.beginPath()
      for (let i = 1; i < 10; i++) { const x = (w * i) / 10; g.moveTo(x, 0); g.lineTo(x, h) }
      for (let j = 1; j < 4; j++) { const y = (h * j) / 4; g.moveTo(0, y); g.lineTo(w, y) }
      g.stroke()

      const d = last.current
      if (!d) return
      const n = d.length
      g.beginPath()
      for (let i = 0; i < n; i++) {
        const x = (i / (n - 1)) * w
        const y = h - (d[i] / 255) * h
        i ? g.lineTo(x, y) : g.moveTo(x, y)
      }
      g.strokeStyle = '#38bdf8'; g.lineWidth = Math.max(1, (window.devicePixelRatio || 1) * 0.9)
      g.stroke()
      g.lineTo(w, h); g.lineTo(0, h); g.closePath()
      g.fillStyle = 'rgba(56,189,248,.12)'; g.fill()
    }
  }, [])

  useEffect(() => bus.on(id, (h, bytes) => {
    last.current = bytes
    if (!meta || meta.sr !== h.sr || meta.fc !== h.fc) setMeta(h)
    const cv = ref.current
    if (cv) draw.current(cv)
  }), [bus, id, meta, ref])

  const lo = meta ? meta.fc - meta.sr / 2 : 0
  const hi = meta ? meta.fc + meta.sr / 2 : 0
  return (
    <Frame title={label} subtitle={meta ? `${fmtHz(meta.sr)} de bande` : '—'}
           right={meta ? `${meta.min_db} → ${meta.max_db} dB` : ''}>
      <canvas ref={ref} className="absolute inset-0 w-full h-full" />
      {meta && (
        <div className="absolute inset-x-0 bottom-0 flex justify-between px-2 pb-0.5
                        text-[10px] text-mut font-mono pointer-events-none">
          <span>{fmtHz(lo)}</span><span>{fmtHz(meta.fc)}</span><span>{fmtHz(hi)}</span>
        </div>
      )}
    </Frame>
  )
}

// -------------------------------------------------------------- waterfall --
function WaterfallView({ id, label }) {
  const { bus } = useApp()
  const [meta, setMeta] = useState(null)
  const ref = useCanvas(null)

  useEffect(() => bus.on(id, (h, bytes) => {
    const cv = ref.current
    if (!cv) return
    if (!meta || meta.sr !== h.sr || meta.fc !== h.fc || meta.palette !== h.palette) setMeta(h)
    const g = cv.getContext('2d', { willReadFrequently: false })
    const { width: w, height: hh } = cv

    // On décale l’image d’un pixel vers le bas, puis on écrit la nouvelle ligne
    // tout en haut. Redessiner l’historique complet à chaque trame coûterait
    // cent fois plus cher.
    g.drawImage(cv, 0, 1)

    const lut = paletteOf(h.palette)
    const row = g.createImageData(w, 1)
    const px = row.data
    const n = bytes.length
    for (let x = 0; x < w; x++) {
      const v = bytes[Math.min(n - 1, Math.floor((x / w) * n))]
      const o = v * 3
      px[x * 4] = lut[o]; px[x * 4 + 1] = lut[o + 1]; px[x * 4 + 2] = lut[o + 2]; px[x * 4 + 3] = 255
    }
    g.putImageData(row, 0, 0)
  }), [bus, id, meta, ref])

  const lo = meta ? meta.fc - meta.sr / 2 : 0
  const hi = meta ? meta.fc + meta.sr / 2 : 0
  return (
    <Frame title={label} subtitle={meta ? `${fmtHz(meta.sr)} de bande` : '—'}
           right={meta?.palette}>
      <canvas ref={ref} className="absolute inset-0 w-full h-full bg-card2" />
      {meta && (
        <div className="absolute inset-x-0 top-0 flex justify-between px-2 pt-0.5
                        text-[10px] text-mut font-mono pointer-events-none
                        [text-shadow:0_1px_2px_#000]">
          <span>{fmtHz(lo)}</span><span>{fmtHz(meta.fc)}</span><span>{fmtHz(hi)}</span>
        </div>
      )}
    </Frame>
  )
}

// -------------------------------------------------------------------- scope --
function ScopeView({ id, label }) {
  const { bus } = useApp()
  const last = useRef(null)
  const [meta, setMeta] = useState(null)
  const draw = useRef(() => {})
  const ref = useCanvas((cv) => draw.current(cv))

  useEffect(() => {
    draw.current = (cv) => {
      const g = cv.getContext('2d')
      const { width: w, height: h } = cv
      g.fillStyle = '#0b1220'; g.fillRect(0, 0, w, h)
      g.strokeStyle = '#16223a'; g.beginPath(); g.moveTo(0, h / 2); g.lineTo(w, h / 2); g.stroke()
      const d = last.current
      if (!d) return
      const n = d.length / 2
      // Échelle automatique : sans elle, un signal faible est une ligne plate.
      let peak = 1e-6
      for (let i = 0; i < d.length; i++) peak = Math.max(peak, Math.abs(d[i]))
      const k = (h / 2) * 0.9 / peak
      for (const [off, color] of [[0, '#38bdf8'], [1, '#f59e0b']]) {
        g.beginPath()
        for (let i = 0; i < n; i++) {
          const x = (i / (n - 1)) * w
          const y = h / 2 - d[i * 2 + off] * k
          i ? g.lineTo(x, y) : g.moveTo(x, y)
        }
        g.strokeStyle = color; g.lineWidth = 1.2; g.stroke()
      }
    }
  }, [])

  useEffect(() => bus.on(id, (h, bytes) => {
    // Copie alignée : la charge utile ne commence pas forcément sur un
    // multiple de 4 octets, et Float32Array l’exige.
    last.current = new Float32Array(bytes.slice().buffer)
    if (!meta) setMeta(h)
    const cv = ref.current
    if (cv) draw.current(cv)
  }), [bus, id, meta, ref])

  return (
    <Frame title={label} subtitle={meta ? `${meta.n} points` : '—'} right="I bleu · Q orange">
      <canvas ref={ref} className="absolute inset-0 w-full h-full" />
    </Frame>
  )
}

// -------------------------------------------------------------------- power --
function PowerView({ id, label }) {
  const { bus } = useApp()
  const [db, setDb] = useState(null)
  useEffect(() => bus.on(id, (h) => setDb(h.db)), [bus, id])
  const pct = db == null ? 0 : Math.max(0, Math.min(1, (db + 120) / 120))
  return (
    <Frame title={label} subtitle="niveau moyen" right="">
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-4">
        <div className="text-3xl font-mono tabular-nums">
          {db == null ? '—' : db.toFixed(1)}<span className="text-mut text-base ml-1">dB</span>
        </div>
        <div className="w-full h-2 rounded-full bg-card2 border border-line overflow-hidden">
          <div className="h-full bg-gradient-to-r from-good via-warn to-bad transition-[width] duration-100"
               style={{ width: `${pct * 100}%` }} />
        </div>
      </div>
    </Frame>
  )
}

const VIEWS = {
  spectrum_sink: SpectrumView,
  waterfall_sink: WaterfallView,
  scope_sink: ScopeView,
  power_sink: PowerView,
}

export default function Displays() {
  const { nodes, catalogue, status } = useApp()
  const sinks = nodes.filter((n) => VIEWS[n.data.btype])

  if (!sinks.length) {
    return (
      <div className="h-full flex items-center justify-center text-mut text-sm">
        Ajoute un afficheur (Spectre, Waterfall, Oscilloscope…) et relie-le à une FFT.
      </div>
    )
  }
  return (
    <div className="h-full flex gap-3 p-3 overflow-x-auto">
      {!status.running && (
        <div className="absolute z-10 left-1/2 -translate-x-1/2 mt-1 text-[11px] text-mut
                        bg-card border border-line rounded-full px-3 py-1">
          graphe arrêté — appuie sur Démarrer
        </div>
      )}
      {sinks.map((n) => {
        const V = VIEWS[n.data.btype]
        return <V key={n.id} id={n.id} label={catalogue[n.data.btype]?.label || n.data.btype} />
      })}
    </div>
  )
}
