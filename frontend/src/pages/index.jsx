import { useCallback, useMemo } from 'react'
import { ReactFlow, Background, Controls } from '@xyflow/react'
import { useApp } from '@/store'
import BlockNode from '@/components/BlockNode'
import BlockPalette from '@/components/BlockPalette'
import ParamsPanel from '@/components/ParamsPanel'
import Displays from '@/components/Displays'

export default function Home() {
  const {
    nodes, edges, ready, error,
    onNodesChange, onEdgesChange, onConnect, setSelected,
  } = useApp()

  // nodeTypes DOIT être stable : recréer l-objet à chaque rendu fait remonter
  // un avertissement de React Flow et recrée tous les blocs à chaque frappe.
  const nodeTypes = useMemo(() => ({ block: BlockNode }), [])
  const onSelectionChange = useCallback(({ nodes: sel }) => {
    setSelected(sel?.[0]?.id ?? null)
  }, [setSelected])

  if (!ready) {
    return (
      <div className="flex-1 flex items-center justify-center text-mut text-sm">
        {error ? <span className="text-bad">{error}</span> : 'Connexion au moteur…'}
      </div>
    )
  }

  return (
    <div className="flex-1 flex min-h-0">
      <BlockPalette />

      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex-1 min-h-0 relative">
          <ReactFlow
            nodes={nodes} edges={edges}
            nodeTypes={nodeTypes}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onSelectionChange={onSelectionChange}
            fitView
            fitViewOptions={{ padding: 0.18 }}
            // Au montage, le conteneur peut encore avoir une hauteur nulle :
            // le cadrage automatique ne cadrerait rien et les blocs les plus à
            // gauche resteraient hors de l’écran. On le refait une fois la
            // mise en page stabilisée.
            onInit={(inst) => requestAnimationFrame(() => inst.fitView({ padding: 0.18 }))}
            proOptions={{ hideAttribution: true }}
            defaultEdgeOptions={{ animated: true }}
          >
            <Background color="#1b2740" gap={22} size={1} />
            <Controls className="!bg-card !border !border-line" showInteractive={false} />
            {/* Pas de MiniMap : sur un graphe de cette taille elle n’apporte rien
                et vient se poser par-dessus les blocs du coin bas-droit. */}
          </ReactFlow>
        </div>

        <div className="h-[300px] shrink-0 border-t border-line bg-bg/60 relative">
          <Displays />
        </div>
      </div>

      <ParamsPanel />
    </div>
  )
}
