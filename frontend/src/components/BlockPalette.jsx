import { useApp } from '@/store'

const ORDER = ['source', 'traitement', 'afficheur', 'divers']
const TITLES = {
  source: 'Sources', traitement: 'Traitement', afficheur: 'Afficheurs', divers: 'Divers',
}
const DOT = { source: 'bg-source', traitement: 'bg-proc', afficheur: 'bg-sink', divers: 'bg-mut' }

// Catalogue à gauche. Il est construit à partir de ce que le backend déclare :
// ajouter un bloc en Python le fait apparaître ici sans toucher au front.
export default function BlockPalette() {
  const { catalogue, addBlock } = useApp()
  const byCat = {}
  for (const b of Object.values(catalogue)) (byCat[b.category] ||= []).push(b)

  return (
    <aside className="w-56 shrink-0 border-r border-line bg-card/40 overflow-y-auto">
      <div className="px-3 py-2 text-[11px] uppercase tracking-wide text-mut border-b border-line">
        Blocs disponibles
      </div>
      {ORDER.filter((c) => byCat[c]).map((cat) => (
        <div key={cat} className="py-2">
          <div className="px-3 pb-1 text-[11px] text-mut flex items-center gap-2">
            <span className={`w-1.5 h-1.5 rounded-full ${DOT[cat]}`} />{TITLES[cat] || cat}
          </div>
          {byCat[cat].map((b) => (
            <button key={b.type} onClick={() => addBlock(b.type)} title={b.doc}
                    className="w-full text-left px-3 py-1.5 hover:bg-card2 transition-colors">
              <div className="text-[13px]">{b.label}</div>
              <div className="text-[10px] text-mut/80 line-clamp-2 leading-snug">{b.doc}</div>
            </button>
          ))}
        </div>
      ))}
      <div className="px-3 py-3 text-[10px] text-mut/70 border-t border-line leading-relaxed">
        Clic pour ajouter. Relie une sortie (orange) à une entrée (bleue) en tirant un fil.
        Suppr. efface la sélection.
      </div>
    </aside>
  )
}
