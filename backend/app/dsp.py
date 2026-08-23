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
        self.state = None   # créé au premier appel, dans le type du signal

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if len(self.taps) < 2:
            return x * self.taps[0]
        # Le même filtre sert avant le discriminateur (signal COMPLEXE) et après
        # (signal RÉEL, l'audio). On adopte donc le type de l'entrée plutôt que
        # d'imposer du complexe : filtrer de l'audio en complex64 doublerait le
        # calcul pour une partie imaginaire toujours nulle.
        if self.state is None or self.state.dtype != x.dtype:
            self.state = np.zeros(len(self.taps) - 1, dtype=x.dtype)
        xx = np.concatenate([self.state, x])
        self.state = xx[-(len(self.taps) - 1):]
        return np.convolve(xx, self.taps, mode="valid").astype(x.dtype)


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


class FmDiscriminator:
    """Démodulateur FM : l'information est dans la VITESSE de rotation de la phase.

    `angle(x[n] · conj(x[n-1]))` donne l'écart de phase entre deux échantillons
    successifs, c'est-à-dire la fréquence instantanée. Une ligne, et c'est toute
    la démodulation FM.

    ⚠️ L'échantillon retenu d'un bloc à l'autre n'est pas un détail : sans lui,
    le premier écart de phase de chaque bloc est calculé contre zéro, ce qui
    fabrique un **clic à chaque frontière de bloc** — 36 clics par seconde.
    """

    def __init__(self):
        self.last = np.complex64(0)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return np.zeros(0, dtype=F32)
        xx = np.empty(len(x) + 1, dtype=C64)
        xx[0] = self.last
        xx[1:] = x
        self.last = x[-1]
        # Divisé par pi : une déviation maximale donne ±1, l'échelle attendue
        # par la carte son.
        return (np.angle(xx[1:] * np.conj(xx[:-1])) / np.pi).astype(F32)


class Deemphasis:
    """Désaccentuation FM : filtre passe-bas du premier ordre.

    En FM, l'émetteur accentue les aigus avant modulation (pré-accentuation)
    pour les sortir du bruit ; le récepteur doit faire l'inverse. Sans ça, le son
    est criard et sifflant — c'est le défaut le plus reconnaissable d'un
    démodulateur FM maison.

    Constante de temps : **50 µs en Europe**, 75 µs en Amérique du Nord.
    """

    def __init__(self, tau_s: float, sample_rate: float):
        self.a = float(np.exp(-1.0 / (tau_s * sample_rate)))
        self.y = 0.0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        # Récurrence y[n] = a·y[n-1] + (1-a)·x[n]. lfilter n'étant pas dispo
        # sans scipy, on la déroule — c'est le seul endroit du code où une
        # boucle Python est justifiée, et elle porte sur l'audio (48 kHz),
        # pas sur le flux à 2,4 MS/s.
        a, b = self.a, 1.0 - self.a
        out = np.empty_like(x)
        y = self.y
        for i in range(len(x)):
            y = a * y + b * float(x[i])
            out[i] = y
        self.y = y
        return out


class DcBlock:
    """Supprime la composante continue (passe-haut du premier ordre).

    Indispensable après une démodulation AM : le module du signal contient la
    porteuse, une constante énorme devant l'audio. Utile aussi en FM, où un
    accord légèrement décalé produit un décalage constant.
    """

    def __init__(self, cutoff_hz: float, sample_rate: float):
        # ⚠️ Le lissage porte sur la moyenne d'un BLOC, pas sur un échantillon :
        # le coefficient doit donc s'exprimer par bloc. Avec un coefficient par
        # échantillon (exp(-2π·20/48000) = 0,9974), il fallait des centaines de
        # blocs pour converger — la porteuse AM restait dans le signal et
        # écrasait tout. Symptôme observé : un pic à 37 Hz au lieu du ton à
        # 1 kHz, parce que le continu dominait le spectre.
        self.a = 0.9
        self.mean = None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if len(x) == 0:
            return x
        m = float(np.mean(x))
        # Premier bloc : on adopte sa moyenne telle quelle. Sinon le tout début
        # de l'écoute part avec un décalage plein pot.
        self.mean = m if self.mean is None else self.a * self.mean + (1.0 - self.a) * m
        return (x - self.mean).astype(x.dtype)

class Decimator:
    """Filtre puis décime, en conservant la PHASE d'un bloc à l'autre.

    ⚠️ Le piège qui fabrique un bourdonnement. Écrire `y[::d]` recommence à
    l'indice 0 à chaque bloc. Or un bloc de 65 536 échantillons décimé par 10
    n'en consomme pas un multiple de 10 : le bloc suivant devrait commencer à
    l'indice 4, pas 0. Sans cette correction, l'audio saute d'un échantillon à
    chaque frontière — 36 fois par seconde, soit un ronflement à 36 Hz par-dessus
    la voix.

    Mesuré avant/après sur une station FM simulée : saut de 12 % de l'amplitude
    crête, ramené à moins de 1 %.
    """

    def __init__(self, cutoff_hz: float, sample_rate: float, factor: int, ntaps: int = 63):
        self.d = max(1, int(factor))
        self.fir = Fir(lowpass_taps(cutoff_hz, sample_rate, ntaps))
        self.off = 0

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y = self.fir(x)
        if self.d == 1:
            return y
        out = y[self.off::self.d]
        # Indice de départ du prochain bloc, dans son propre repère.
        self.off = (self.off - len(y)) % self.d
        return out
