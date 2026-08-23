// Lecture audio côté navigateur.
//
// ⚠️ Contrainte non négociable des navigateurs : un AudioContext créé sans geste
// de l'utilisateur démarre « suspendu », et RIEN NE SORT — sans le moindre
// message d'erreur. D'où le bouton « Écouter » : il n'est pas décoratif, c'est
// lui qui autorise le son. C'est la première cause de « ça ne marche pas » sur
// ce genre de projet.
export function creerLecteur() {
  let ctx = null;
  let node = null;
  let gain = null;
  let etat = { remplissage_ms: 0, correction_ppm: 0, sousAlim: 0, ctxRate: 0, srcRate: 0 };
  const abonnes = new Set();

  async function demarrer() {
    if (ctx) {
      // Un contexte déjà créé peut avoir été suspendu par l'onglet en arrière-plan.
      if (ctx.state === 'suspended') await ctx.resume();
      return true;
    }
    // On DEMANDE 48 000 : si le navigateur accepte, le pas de lecture vaut
    // exactement 1 et l'interpolation ne fait rien. S'il refuse (44 100 sur
    // certaines machines), le worklet s'en accommode tout seul.
    ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
    await ctx.audioWorklet.addModule('/audio-processor.js');
    node = new AudioWorkletNode(ctx, 'blocksdr-audio', { outputChannelCount: [1] });
    gain = ctx.createGain();
    node.connect(gain).connect(ctx.destination);
    node.port.onmessage = (e) => {
      if (e.data?.type === 'etat') {
        etat = e.data;
        for (const cb of abonnes) cb(etat);
      }
    };
    if (ctx.state === 'suspended') await ctx.resume();
    return ctx.state === 'running';
  }

  return {
    demarrer,
    enMarche: () => !!ctx && ctx.state === 'running',
    // Reçoit la charge utile brute du WebSocket (Int16 entrelacé mono).
    pousser(bytes, sr) {
      if (!node) return;
      // Copie alignée : la charge utile ne commence pas forcément sur un
      // multiple de 2 octets dans le tampon d'origine, et Int16Array l'exige.
      const pcm = new Int16Array(bytes.slice().buffer);
      node.port.postMessage({ type: 'audio', pcm, sr }, [pcm.buffer]);
    },
    setVolume(v) { if (gain) gain.gain.value = v; },
    onEtat(cb) { abonnes.add(cb); return () => abonnes.delete(cb); },
    etat: () => etat,
    async arreter() {
      if (ctx) { await ctx.close(); ctx = node = gain = null; }
    },
  };
}
