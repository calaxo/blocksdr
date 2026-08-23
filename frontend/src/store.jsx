import { createContext, useContext, useEffect, useMemo, useRef, useState, useCallback } from 'react'
import { applyEdgeChanges, applyNodeChanges, addEdge } from '@xyflow/react'
import * as api from '@/lib/api'
import { createBus } from '@/lib/bus'

// État global : le catalogue de blocs, le graphe en cours d’édition, et le
// statut du moteur. Les TRAMES de données ne passent pas par ici (voir bus.js) :
// seul ce qui change rarement est dans React.
const Ctx = createContext(null)
export const useApp = () => useContext(Ctx)

// ---- conversions entre le format du backend et celui de React Flow --------
// Le backend raisonne en « from/fromPort », React Flow en
// « source/sourceHandle ». On traduit aux deux frontières plutôt que
// d’imposer le vocabulaire de l’un à l’autre.
const toFlowNodes = (nodes) =>
  nodes.map((n) => ({
    id: n.id,
    type: 'block',
    position: { x: n.x ?? 0, y: n.y ?? 0 },
    data: { btype: n.type, params: n.params || {} },
  }))

const toFlowEdges = (edges) =>
  edges.map((e, i) => ({
    id: `e${i}-${e.from}-${e.to}`,
    source: e.from, sourceHandle: e.fromPort,
    target: e.to, targetHandle: e.toPort,
    animated: true,
  }))

const fromFlow = (nodes, edges) => ({
  nodes: nodes.map((n) => ({
    id: n.id, type: n.data.btype,
    x: Math.round(n.position.x), y: Math.round(n.position.y),
    params: n.data.params || {},
  })),
  edges: edges.map((e) => ({
    from: e.source, fromPort: e.sourceHandle || 'out',
    to: e.target, toPort: e.targetHandle || 'in',
  })),
})

let seq = 0
const newId = (type) => `${type}_${(++seq).toString(36)}${Date.now().toString(36).slice(-3)}`

export function AppProvider({ children }) {
  const [catalogue, setCatalogue] = useState({})
  const [nodes, setNodes] = useState([])
  const [edges, setEdges] = useState([])
  const [status, setStatus] = useState({ running: false })
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState(null)
  const [selected, setSelected] = useState(null)
  const [ready, setReady] = useState(false)

  const bus = useMemo(() => createBus(), [])
  const syncTimer = useRef(null)
  const dirty = useRef(false)

  // -- chargement initial --------------------------------------------------
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const [b, g] = await Promise.all([api.getBlocks(), api.getGraph()])
        if (!alive) return
        setCatalogue(Object.fromEntries(b.blocks.map((x) => [x.type, x])))
        setNodes(toFlowNodes(g.graph.nodes))
        setEdges(toFlowEdges(g.graph.edges))
        setStatus(g.status)
        setReady(true)
      } catch (e) {
        setError(`Backend injoignable : ${e.message}`)
      }
    })()
    return () => { alive = false }
  }, [])

  // -- statut poussé par le serveur ---------------------------------------
  useEffect(() => {
    return bus.onStatus((m) => {
      if (m.type === 'socket') setConnected(m.connected)
      else if (m.type === 'stats') setStatus((s) => ({ ...s, ...m }))
      else if (m.type === 'error') setError(m.message)
      else if (m.type === 'hello') setStatus(m.status)
    })
  }, [bus])

  // -- envoi du graphe, groupé -------------------------------------------
  // Chaque modification (déplacement, paramètre, liaison) reconstruit le graphe
  // côté serveur. On attend 400 ms de calme pour ne pas le reconstruire à
  // chaque pixel de déplacement d’un bloc.
  const scheduleSync = useCallback((ns, es) => {
    dirty.current = true
    clearTimeout(syncTimer.current)
    syncTimer.current = setTimeout(async () => {
      try {
        const r = await api.putGraph(...Object.values(fromFlow(ns, es)))
        setError(r.ok ? null : r.error)
        if (r.status) setStatus(r.status)
      } catch (e) {
        setError(e.message)
      }
      dirty.current = false
    }, 400)
  }, [])

  const commit = useCallback((ns, es) => {
    setNodes(ns); setEdges(es); scheduleSync(ns, es)
  }, [scheduleSync])

  // -- actions du graphe ---------------------------------------------------
  const onNodesChange = useCallback((changes) => {
    setNodes((ns) => {
      const next = applyNodeChanges(changes, ns)
      // On ne renvoie au serveur que les changements structurels ; un simple
      // survol ou une sélection ne doit pas relancer le graphe.
      if (changes.some((c) => c.type === 'remove' || (c.type === 'position' && !c.dragging))) {
        setEdges((es) => { scheduleSync(next, es); return es })
      }
      return next
    })
  }, [scheduleSync])

  const onEdgesChange = useCallback((changes) => {
    setEdges((es) => {
      const next = applyEdgeChanges(changes, es)
      if (changes.some((c) => c.type === 'remove')) setNodes((ns) => { scheduleSync(ns, next); return ns })
      return next
    })
  }, [scheduleSync])

  const onConnect = useCallback((conn) => {
    setEdges((es) => {
      const next = addEdge({ ...conn, animated: true }, es)
      setNodes((ns) => { scheduleSync(ns, next); return ns })
      return next
    })
  }, [scheduleSync])

  const addBlock = useCallback((btype, position) => {
    const spec = catalogue[btype]
    if (!spec) return
    const id = newId(btype)
    const params = Object.fromEntries(spec.params.map((p) => [p.name, p.default]))
    const node = { id, type: 'block', position: position || { x: 120 + Math.random() * 240, y: 80 + Math.random() * 240 }, data: { btype, params } }
    setNodes((ns) => { const next = [...ns, node]; setEdges((es) => { scheduleSync(next, es); return es }); return next })
    setSelected(id)
  }, [catalogue, scheduleSync])

  const setParam = useCallback((id, name, value) => {
    setNodes((ns) => {
      const next = ns.map((n) => n.id === id
        ? { ...n, data: { ...n.data, params: { ...n.data.params, [name]: value } } } : n)
      setEdges((es) => { scheduleSync(next, es); return es })
      return next
    })
  }, [scheduleSync])

  const removeNode = useCallback((id) => {
    setNodes((ns) => {
      const next = ns.filter((n) => n.id !== id)
      setEdges((es) => {
        const e2 = es.filter((e) => e.source !== id && e.target !== id)
        scheduleSync(next, e2)
        return e2
      })
      return next
    })
    setSelected((s) => (s === id ? null : s))
  }, [scheduleSync])

  // -- marche / arrêt ------------------------------------------------------
  const toggleRun = useCallback(async () => {
    try {
      const r = await api.setRunning(!status.running)
      setError(r.ok ? null : r.error)
      if (r.status) setStatus(r.status)
    } catch (e) { setError(e.message) }
  }, [status.running])

  const value = {
    catalogue, nodes, edges, status, connected, error, selected, ready, bus,
    setSelected, onNodesChange, onEdgesChange, onConnect,
    addBlock, setParam, removeNode, toggleRun, commit,
  }
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}
