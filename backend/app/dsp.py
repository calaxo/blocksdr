"""Briques de traitement du signal, en numpy pur.

Volontairement sans scipy : tout ce dont on a besoin ici (filtre RIF, oscillateur
local, FFT fenêtrée) tient en quelques dizaines de lignes, et ça évite une
dépendance lourde à compiler dans l'image Docker.

Les objets de ce module sont *à état* : ils sont appelés bloc après bloc sur des
morceaux successifs du flux, et doivent donc mémoriser ce qui dépasse d'un bloc
à l'autre (queue du filtre, phase de l'oscillateur). C'est toute la difficulté
du traitement en flux par rapport au traitement d'un fichier entier.
"""

from __future__ import annotations

import numpy as np

C64 = np.complex64
F32 = np.float32


def lowpass_taps(cutoff_hz: float, sample_rate: float, ntaps: int = 63) -> np.ndarray:
    """Coefficients d'un filtre passe-bas RIF (sinus cardinal fenêtré).

    `cutoff_hz` est la fréquence de coupure, `sample_rate` la cadence du signal
    d'entrée. Un nombre de coefficients impair garantit un retard entier.
    """
    ntaps = int(ntaps) | 1  # force l'imparité
    fc = float(np.clip(cutoff_hz / sample_rate, 1e-4, 0.499))
    n = np.arange(ntaps) - (ntaps - 1) / 2.0
    h = 2.0 * fc * np.sinc(2.0 * fc * n)
    h *= np.hamming(ntaps)
    h /= np.sum(h)
    return h.astype(F32)


class Fir:
    """Filtre RIF à état, pour un traitement en flux continu.

    On conserve les `ntaps-1` derniers échantillons du bloc précédent et on les
    remet devant le bloc suivant : sans ça, on entendrait un « clic » à chaque
    frontière de bloc, parce que le filtre repartirait de zéro à chaque fois.
    """

    def __init__(self, taps: np.ndarray):
        self.taps = np.asarray(taps, dtype=F32)
        self.state = np.zeros(len(self.taps) - 1, dtype=C64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if len(self.taps) < 2:
            return x * self.taps[0]
        xx = np.concatenate([self.state, x.astype(C64, copy=False)])
        self.state = xx[-(len(self.taps) - 1):]
        return np.convolve(xx, self.taps, mode="valid").astype(C64)


class Nco:
    """Oscillateur local : décale tout le spectre de `offset` hertz.

    Multiplier un signal complexe par exp(-j·2π·f·t) revient à faire glisser
    l'axe des fréquences. C'est ainsi qu'on « amène au centre » une station qui
    se trouve sur le bord de la fenêtre captée.

    La phase est mémorisée d'un bloc à l'autre, sinon la sinusoïde repart de
    zéro à chaque bloc et fabrique des parasites.
    """

    def __init__(self):
        self.phase = 0.0

    def __call__(self, x: np.ndarray, offset_hz: float, sample_rate: float) -> np.ndarray:
        if offset_hz == 0.0:
            return x
        n = len(x)
        step = -2.0 * np.pi * offset_hz / sample_rate
        ph = self.phase + step * np.arange(1, n + 1, dtype=np.float64)
        self.phase = float(ph[-1] % (2.0 * np.pi))
        return (x * np.exp(1j * ph)).astype(C64)


WINDOWS = {
    "hann": np.hanning,
    "hamming": np.hamming,
    "blackman": np.blackman,
    "rect": np.ones,
}


MAX_SEGMENTS = 8


def spectrum_db(x: np.ndarray, size: int, window: str = "hann") -> np.ndarray:
    """Spectre moyenné en dB, prêt à afficher.

    Le bloc reçu est découpé en segments de `size` échantillons, dont on moyenne
    les spectres (méthode de Welch) : le tracé est bien plus lisible qu'avec une
    FFT unique, où le bruit saute dans tous les sens.

    `fftshift` remet la fréquence centrale au milieu du tableau — sans lui, le
    spectre est affiché « coupé en deux et interverti », ce qui déroute toujours
    au premier essai.
    """
    size = int(size)
    if len(x) < size:
        x = np.pad(x, (0, size - len(x)))
    # ⚠️ On BORNE le nombre de segments moyennés. Mesuré sur le vrai dongle à
    # 2,4 MS/s avec des blocs de 65 536 : sans borne, cela fait 32 FFT par tour,
    # soit ~1 150 FFT de 2 048 points par seconde — 98 % d'un cœur pour un
    # affichage qui ne montrera jamais la différence. Huit segments suffisent
    # largement à calmer le bruit, et divisent le coût par quatre.
    nseg = max(1, min(MAX_SEGMENTS, len(x) // size))
    win = WINDOWS.get(window, np.hanning)(size).astype(F32)
    # Correction de gain de la fenêtre : sans elle, changer de fenêtre décale
    # tout le niveau affiché de plusieurs dB, et on croit à une panne de gain.
    norm = np.sum(win) or 1.0
    acc = np.zeros(size, dtype=np.float64)
    for k in range(nseg):
        seg = x[k * size:(k + 1) * size]
        X = np.fft.fftshift(np.fft.fft(seg * win))
        acc += (np.abs(X) / norm) ** 2
    acc /= nseg
    return (10.0 * np.log10(acc + 1e-20)).astype(F32)
