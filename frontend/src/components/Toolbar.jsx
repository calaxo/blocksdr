import { useApp } from '@/store'

// Barre du haut : marche/arrêt, santé de la liaison, et les deux chiffres qui
// disent si le graphe tient la cadence (tours/s et charge processeur).
export default function Toolbar() {
  const { status, connected, error, toggleRun } = useApp()
  const running = !!status.running

  return (
    <header className="shrink-0 border-b border-line bg-card/70 backdrop-blur px-4 py-2.5
                       flex items-center gap-4">
      <div className="flex items-baseline gap-2">
        <span className="text-base font-semibold tracking-tight">BlockSDR</span>
        <span className="text-[11px] text-mut">graphe de blocs · RTL-SDR</span>
      </div>

      <button onClick={toggleRun}
              className={`btn ${running ? 'btn-danger' : 'btn-primary'}`}>
        <span className={`inline-block w-2 h-2 rounded-full ${running ? 'bg-bad' : 'bg-good'}`} />
        {running ? 'Arrêter' : 'Démarrer'}
      </button>

      <div className="flex items-center gap-3 text-[12px] text-mut">
        <span className="flex items-center gap-1.5">
          <span className={`inline-block w-2 h-2 rounded-full ${connected ? 'bg-good' : 'bg-bad'}`} />
          {connected ? 'flux connecté' : 'flux coupé'}
        </span>
        {running && (
          <>
            <span className="chip">{(status.rate ?? 0).toFixed(1)} tours/s</span>
            <span className={`chip ${status.load > 0.85 ? 'text-bad border-bad/50' : ''}`}>
              charge {Math.round((status.load ?? 0) * 100)} %
            </span>
          </>
        )}
      </div>

      {error && (
        <div className="ml-auto text-[12px] text-bad bg-bad/10 border border-bad/30
                        rounded-lg px-3 py-1 max-w-[46vw] truncate" title={error}>
          {error}
        </div>
      )}
    </header>
  )
}
