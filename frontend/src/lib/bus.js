// Réception du flux temps réel.
//
// Les trames de données n'entrent PAS dans l'état React : à 25 images par
// seconde et par afficheur, un setState par trame ferait s'effondrer le rendu.
// On passe donc par un petit bus d'événements, et chaque afficheur dessine
// lui-même dans son canvas. React ne gère que ce qui change rarement (le
// graphe, les statuts).
//
// Format d'une trame binaire, côté serveur :
//   [uint32 LE : longueur de l’en-tête][en-tête JSON][charge utile]
export function createBus() {
  const subs = new Map()          // idAfficheur -> Set(callback)
  const statusSubs = new Set()
  let ws = null
  let retry = null
  let closed = false

  const dec = new TextDecoder()

  function emit(header, payload) {
    const set = subs.get(header.sink)
    if (set) for (const cb of set) cb(header, payload)
  }

  function connect() {
    if (closed) return
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    ws = new WebSocket(`${proto}//${location.host}/ws`)
    ws.binaryType = 'arraybuffer'

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let msg
        try { msg = JSON.parse(ev.data) } catch { return }
        for (const cb of statusSubs) cb(msg)
        return
      }
      const buf = ev.data
      const view = new DataView(buf)
      const hlen = view.getUint32(0, true)
      const header = JSON.parse(dec.decode(new Uint8Array(buf, 4, hlen)))
      const bytes = new Uint8Array(buf, 4 + hlen)
      emit(header, bytes)
    }
    ws.onclose = () => {
      for (const cb of statusSubs) cb({ type: 'socket', connected: false })
      if (!closed) retry = setTimeout(connect, 1200)   // reconnexion automatique
    }
    ws.onopen = () => {
      for (const cb of statusSubs) cb({ type: 'socket', connected: true })
    }
  }

  connect()

  return {
    // Abonnement d’un afficheur à ses propres trames.
    on(sinkId, cb) {
      if (!subs.has(sinkId)) subs.set(sinkId, new Set())
      subs.get(sinkId).add(cb)
      return () => subs.get(sinkId)?.delete(cb)
    },
    onStatus(cb) {
      statusSubs.add(cb)
      return () => statusSubs.delete(cb)
    },
    close() {
      closed = true
      clearTimeout(retry)
      ws?.close()
    },
  }
}
