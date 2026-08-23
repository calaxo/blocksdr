// Accès au backend Python. Le proxy Vite renvoie /api vers uvicorn, donc les
// URL sont identiques en développement et en production.
async function req(url, opts) {
  const r = await fetch(url, opts)
  if (!r.ok) throw new Error(`${url} -> ${r.status}`)
  return r.json()
}

export const getBlocks = () => req('/api/blocks')
export const getGraph = () => req('/api/graph')

export const putGraph = (nodes, edges) =>
  req('/api/graph', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ nodes, edges }),
  })

export const setRunning = (running) =>
  req('/api/run', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ running }),
  })
