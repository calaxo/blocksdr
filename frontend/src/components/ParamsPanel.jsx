import { useApp } from '@/store'

// Le formulaire est ENGENDRÉ à partir du schéma déclaré côté Python : aucun
// champ n’est écrit à la main ici. Ajouter un paramètre dans un bloc suffit à
// le faire apparaître, avec son libellé, son unité et son aide.
function Field({ node, p }) {
  const { setParam } = useApp()
  const value = node.data.params?.[p.name] ?? p.default
  const set = (v) => setParam(node.id, p.name, v)

  if (p.type === 'select') {
    return (
      <select className="field" value={String(value)}
              onChange={(e) => {
                const raw = e.target.value
                const opt = p.options.find((o) => String(o.value) === raw)
                set(typeof opt?.value === 'number' ? Number(raw) : raw)
              }}>
        {p.options.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
      </select>
    )
  }
  if (p.type === 'bool') {
    return (
      <button className={`btn w-full justify-center ${value ? 'btn-primary' : ''}`}
              onClick={() => set(!value)}>{value ? 'activé' : 'désactivé'}</button>
    )
  }
  if (p.type === 'freq') {
    // Saisie en MHz : personne ne veut taper 100000000.
    return (
      <div className="flex items-center gap-2">
        <input type="number" className="field font-mono" step="0.0001"
               value={Number(value) / 1e6}
               onChange={(e) => set(Math.round(Number(e.target.value) * 1e6))} />
        <span className="text-mut text-xs">MHz</span>
      </div>
    )
  }
  const num = p.type === 'int' || p.type === 'float'
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2">
        <input type={num ? 'number' : 'text'} className="field font-mono" value={value}
               step={p.step ?? (p.type === 'int' ? 1 : 'any')}
               min={p.min} max={p.max}
               onChange={(e) => set(num ? Number(e.target.value) : e.target.value)} />
        {p.unit && <span className="text-mut text-xs whitespace-nowrap">{p.unit}</span>}
      </div>
      {num && p.min !== undefined && p.max !== undefined && (
        <input type="range" className="w-full accent-accent" min={p.min} max={p.max}
               step={p.step ?? (p.type === 'int' ? 1 : (p.max - p.min) / 100)}
               value={value} onChange={(e) => set(Number(e.target.value))} />
      )}
    </div>
  )
}

export default function ParamsPanel() {
  const { nodes, catalogue, selected, removeNode } = useApp()
  const node = nodes.find((n) => n.id === selected)

  if (!node) {
    return (
      <aside className="w-80 shrink-0 border-l border-line bg-card/40 p-4 text-sm text-mut">
        Clique sur un bloc pour voir ses réglages.
      </aside>
    )
  }
  const spec = catalogue[node.data.btype]

  return (
    <aside className="w-80 shrink-0 border-l border-line bg-card/40 overflow-y-auto">
      <div className="px-4 py-3 border-b border-line">
        <div className="text-sm font-semibold">{spec?.label}</div>
        <div className="text-[10px] font-mono text-mut">{node.id}</div>
        {spec?.doc && <p className="mt-2 text-[11px] text-mut leading-relaxed">{spec.doc}</p>}
      </div>

      <div className="p-4 space-y-4">
        {spec?.params.map((p) => (
          <div key={p.name}>
            <label className="label">{p.label}</label>
            <Field node={node} p={p} />
            {p.help && <p className="mt-1 text-[10px] text-mut/80 leading-snug">{p.help}</p>}
          </div>
        ))}
        {!spec?.params.length && <p className="text-mut text-xs">Ce bloc n’a aucun réglage.</p>}
      </div>

      <div className="p-4 border-t border-line">
        <button className="btn btn-danger w-full justify-center"
                onClick={() => removeNode(node.id)}>Supprimer ce bloc</button>
      </div>
    </aside>
  )
}
