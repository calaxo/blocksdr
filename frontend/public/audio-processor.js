// AudioWorklet de BlockSDR — le morceau difficile de tout le projet.
//
// Le problème : le dongle cadence ses échantillons sur SON quartz, la carte son
// consomme sur LE SIEN. Les deux dérivent l'un par rapport à l'autre de quelques
// ppm. Sans rien faire, le tampon finit par se vider (clic) ou par déborder (la
// latence enfle sans fin). Ce n'est pas une question de radio, c'est de l'audio
// temps réel, et ça arrive quoi qu'on fasse.
//
// La réponse : on ne lit PAS le tampon échantillon par échantillon, mais à un
// pas fractionnaire, avec interpolation linéaire. Ce pas est asservi au
// remplissage du tampon par une boucle très lente :
//
//     pas = (cadence_source / cadence_carte_son) × (1 + correction)
//
// ⭐ Le même mécanisme absorbe gratuitement un AudioContext à 44 100 au lieu de
// 48 000 : le pas nominal vaut alors 1,088 au lieu de 1,000, et il n'y a aucun
// cas particulier à écrire.

const SECONDES_TAMPON = 4.0;      // capacité du tampon circulaire
const CIBLE_S = 0.25;             // remplissage visé : compromis latence/robustesse
const CORRECTION_MAX = 0.005;     // ±0,5 % — au-delà, la hauteur du son s'entend
const GAIN_BOUCLE = 0.02;         // très lent : la dérive se compte en ppm

class BlockSdrProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capacite = Math.ceil(sampleRate * SECONDES_TAMPON);
    this.buf = new Float32Array(this.capacite);
    this.ecriture = 0;        // indice d'écriture (entier)
    this.lecture = 0;         // indice de lecture (FRACTIONNAIRE)
    this.dispo = 0;           // échantillons en attente
    this.srcRate = sampleRate;
    this.volume = 1;
    this.actif = false;
    this.sousAlim = 0;        // nombre de fois où le tampon s'est vidé
    this.compteur = 0;

    this.port.onmessage = (e) => {
      const m = e.data;
      if (m.type === 'audio') {
        this.pousser(m.pcm, m.sr);
      } else if (m.type === 'volume') {
        this.volume = m.value;
      } else if (m.type === 'reset') {
        this.dispo = 0; this.lecture = this.ecriture = 0; this.sousAlim = 0;
      }
    };
  }

  pousser(int16, sr) {
    if (sr) this.srcRate = sr;
    const n = int16.length;
    // Débordement : on jette le plus ancien plutôt que le plus récent. Sur une
    // écoute en direct, le retard est pire que le trou.
    if (this.dispo + n > this.capacite) {
      const trop = this.dispo + n - this.capacite;
      this.lecture = (this.lecture + trop) % this.capacite;
      this.dispo -= trop;
    }
    for (let i = 0; i < n; i++) {
      this.buf[this.ecriture] = int16[i] / 32768;
      this.ecriture = (this.ecriture + 1) % this.capacite;
    }
    this.dispo += n;
    this.actif = true;
  }

  process(_inputs, outputs) {
    const sortie = outputs[0][0];
    if (!sortie) return true;
    const n = sortie.length;

    if (!this.actif || this.dispo < 2) {
      sortie.fill(0);
      if (this.actif) this.sousAlim++;
      return true;
    }

    // Asservissement : on compare le remplissage à la cible et on ajuste le pas.
    const cible = this.srcRate * CIBLE_S;
    const ecart = (this.dispo - cible) / cible;
    let correction = GAIN_BOUCLE * ecart;
    if (correction > CORRECTION_MAX) correction = CORRECTION_MAX;
    if (correction < -CORRECTION_MAX) correction = -CORRECTION_MAX;
    const pas = (this.srcRate / sampleRate) * (1 + correction);

    for (let i = 0; i < n; i++) {
      if (this.dispo < 2) { sortie[i] = 0; this.sousAlim++; continue; }
      const i0 = Math.floor(this.lecture) % this.capacite;
      const i1 = (i0 + 1) % this.capacite;
      const frac = this.lecture - Math.floor(this.lecture);
      // Interpolation linéaire : c'est elle qui permet un pas non entier.
      sortie[i] = (this.buf[i0] * (1 - frac) + this.buf[i1] * frac) * this.volume;
      const avant = Math.floor(this.lecture);
      this.lecture += pas;
      const consomme = Math.floor(this.lecture) - avant;
      if (consomme > 0) {
        this.dispo -= consomme;
        if (this.lecture >= this.capacite) this.lecture -= this.capacite;
      }
    }

    // On remonte l'état à l'interface deux fois par seconde environ.
    this.compteur += n;
    if (this.compteur >= sampleRate / 2) {
      this.compteur = 0;
      this.port.postMessage({
        type: 'etat',
        remplissage_ms: Math.round((this.dispo / this.srcRate) * 1000),
        correction_ppm: Math.round(correction * 1e6),
        sousAlim: this.sousAlim,
        srcRate: this.srcRate,
        ctxRate: sampleRate,
      });
    }
    return true;
  }
}

registerProcessor('blocksdr-audio', BlockSdrProcessor);
