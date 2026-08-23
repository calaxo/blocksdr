import { Handle, Position } from '@xyflow/react'
import { useApp } from '@/store'

const TINT = {
  source: { ring: 'border-source/60', bar: 'bg-source', text: 'text-source' },
  traitement: { ring: 'border-proc/60', bar: 'bg-proc', text: 'text-proc' },
  afficheur: { ring: 'border-sink/60', bar: 'bg-sink', text: 'text-sink' },
  divers: { ring: 'border-line', bar: 'bg-mut', text: 'text-mut' },
}

// Un bloc du graphe. On affiche le nom, les ports, et deux ou trois paramètres
// clés directement sur le bloc : c’est ce qui permet de lire une chaîne d’un
// coup d’œil sans cliquer partout, comme dans GNU Radio Companion.
export default function BlockNode({ id, data, selected }) {
  const { catalogue } = useApp()
  const spec = catalogue[data.btype]
  if (!spec) return <div className="panel px-3 py-2 text-bad text-xs">bloc inconnu : {data.btype}</div>

  const tint = TINT[spec.category] || TINT.divers
  const shown = spec.params.slice(0, 3)

  const fmt = (p) => {
    const v = data.params?.[p.name] ?? p.default
    if (p.type === 'freq') return `${(Number(v) / 1e6).toFixed(4)} MHz`
    if (p.type === 'select') return String(p.options?.find((o) => String(o.value) === String(v))?.label ?? v)
    if (typeof v === 'number' && Math.abs(v) >= 10000) return v.toLocaleString('fr-FR')
    return String(v)
  }

  return (
    <div className={`panel min-w-[190px] overflow-hidden transition-shadow
                     ${selected ? 'border-accent ring-1 ring-accent/40' : tint.ring}`}>
      <div className={`h-1 ${tint.bar}`} />
      <div className="px-3 pt-2 pb-1">
        <div className={`text-[13px] font-semibold ${tint.text}`}>{spec.label}</div>
        <div className="text-[10px] text-mut/70 font-mono">{id}</div>
      </div>

      <div className="px-3 pb-2 space-y-0.5">
        {shown.map((p) => (
          <div key={p.name} className="flex justify-between gap-3 text-[11px]">
            <span className="text-mut truncate">{p.label}</span>
            <span className="font-mono text-ink/90 whitespace-nowrap">{fmt(p)}</span>
          </div>
        ))}
      </div>

      {spec.inputs.map((p, i) => (
        <Handle key={p.name} id={p.name} type="target" position={Position.Left}
                style={{ top: 34 + i * 18, width: 10, height: 10, background: '#38bdf8' }} />
      ))}
      {spec.outputs.map((p, i) => (
        <Handle key={p.name} id={p.name} type="source" position={Position.Right}
                style={{ top: 34 + i * 18, width: 10, height: 10, background: '#f59e0b' }} />
      ))}
    </div>
  )
}
