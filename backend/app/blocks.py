"""Catalogue de blocs, à la GNU Radio.

Chaque bloc déclare :
  - ses **ports** d'entrée/sortie et le type de données qui y circule ;
  - ses **paramètres**, avec un schéma que le front transforme automatiquement
    en formulaire (aucun formulaire n'est écrit à la main côté React) ;
  - une méthode `work()` appelée à chaque tour d'horloge du graphe.

Ce qui circule entre les blocs est un `Signal` : un tableau numpy **plus ses
métadonnées** (cadence d'échantillonnage, fréquence centrale). C'est ce qui
permet à un afficheur situé après une décimation de savoir tout seul quelle
largeur de bande il représente — exactement comme GNU Radio propage son
`samp_rate`.
"""

from __future__ import annotations

import math
import queue
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from . import rtl
from .dsp import (C64, F32, DcBlock, Decimator, Deemphasis, FmDiscriminator,
                  Nco, spectrum_db)


@dataclass
class Signal:
    """Un morceau de flux + son contexte."""
    data: np.ndarray
    sr: float           # échantillons par seconde
    fc: float           # fréquence centrale, en Hz
    kind: str = "iq"    # "iq" (complexe) ou "spectrum" (réel, en dB)


# --------------------------------------------------------------------------
#  Infrastructure
# --------------------------------------------------------------------------

REGISTRY: dict[str, type["Block"]] = {}


def register(cls):
    REGISTRY[cls.type] = cls
    return cls


def P(name, label, ptype, default, **kw) -> dict:
    """Raccourci pour décrire un paramètre (le front en fait un champ).

    `min`/`max` sont montrés par le front (curseur). `bounds=(bas, haut)` est
    une limite que SEUL le serveur applique : elle borne une valeur sans faire
    apparaître de curseur dans le formulaire.
    """
    d = {"name": name, "label": label, "type": ptype, "default": default}
    d.update(kw)
    return d


class ParamError(ValueError):
    """Valeur de paramètre refusée (type, bornes ou option inconnue)."""


def check_param(block_label: str, d: dict, v: Any) -> Any:
    """Vérifie UNE valeur envoyée par le client contre la déclaration `d`.

    Rend la valeur normalisée (un 2048 reçu pour une option déclarée 2048 reste
    un entier ; un « 30 » reste la chaîne attendue par le bloc), ou lève
    `ParamError` avec un message lisible dans l'interface.
    """
    where = f"{block_label} · {d['label']}"
    t = d["type"]
    if t == "select":
        for o in d["options"]:
            if str(o["value"]) == str(v) and not isinstance(v, (dict, list)):
                return o["value"]
        raise ParamError(f"{where} : valeur « {str(v)[:40]} » non proposée")
    if t == "bool":
        if not isinstance(v, bool):
            raise ParamError(f"{where} : vrai/faux attendu")
        return v
    if t not in ("int", "float", "freq"):
        raise ParamError(f"{where} : type de paramètre inconnu")
    # bool est un int en Python : on le refuse explicitement.
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ParamError(f"{where} : nombre attendu")
    if not math.isfinite(v):
        raise ParamError(f"{where} : nombre fini attendu")
    if t == "int":
        if v != int(v):
            raise ParamError(f"{where} : nombre entier attendu")
        v = int(v)
    else:
        v = float(v)
    lo, hi = d.get("bounds", (d.get("min"), d.get("max")))
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ParamError(f"{where} : {v:g} hors limites ({lo} à {hi})")
    return v


class Block:
    type = "block"
    label = "Bloc"
    category = "divers"
    color = "slate"
    doc = ""
    inputs: list[dict] = []
    outputs: list[dict] = []
    params: list[dict] = []

    def __init__(self, node_id: str, params: dict[str, Any], hardware: bool = True):
        self.id = node_id
        self.p = {d["name"]: d["default"] for d in self.params}
        # Les clés inconnues sont ignorées (comme avant) ; les connues sont
        # VÉRIFIÉES : type, bornes, valeur parmi les options.
        for d in self.params:
            if d["name"] in (params or {}):
                self.p[d["name"]] = check_param(self.label, d, params[d["name"]])
        # `hardware=False` : on vérifie le graphe sans toucher au matériel.
        self.hardware = hardware
        self.error: str | None = None
        self.setup()

    # à surcharger
    def setup(self) -> None: ...
    def work(self, ins: dict[str, Signal], ctx: "Ctx") -> dict[str, Signal]:
        return {}
    def close(self) -> None: ...

    @classmethod
    def describe(cls) -> dict:
        return {
            "type": cls.type, "label": cls.label, "category": cls.category,
            "color": cls.color, "doc": cls.doc,
            "inputs": cls.inputs, "outputs": cls.outputs, "params": cls.params,
        }


@dataclass
class Ctx:
    """Contexte d'exécution passé aux blocs à chaque tour."""
    emit: Callable[[dict, bytes], None]
    tick: int = 0
    now: float = 0.0


# --------------------------------------------------------------------------
#  Sources
# --------------------------------------------------------------------------

SCENES = [
    {"value": "fm_band", "label": "Bande FM (3 stations)"},
    {"value": "two_tones", "label": "Deux porteuses pures"},
    {"value": "sweep", "label": "Balayage (trait oblique)"},
    {"value": "burst", "label": "Salves intermittentes"},
    {"value": "noise", "label": "Bruit seul"},
]


@register
class SimSource(Block):
    type = "sim_source"
    label = "Source simulée"
    category = "source"
    color = "amber"
    doc = ("Fabrique un flux I/Q synthétique : de quoi développer et tester "
           "sans dongle branché. Chaque scène produit une figure reconnaissable "
           "sur le waterfall.")
    outputs = [{"name": "out", "dtype": "iq"}]
    params = [
        P("sample_rate", "Cadence d'échantillonnage", "float", 1_024_000.0, unit="éch/s",
          bounds=(1_000, 10_000_000),
          help="Largeur de la fenêtre observée. Un RTL-SDR réel monte à 2 400 000."),
        P("center_freq", "Fréquence centrale", "freq", 100_000_000.0, unit="Hz",
          bounds=(0, 6_000_000_000)),
        P("block_size", "Taille de bloc", "int", 16384, min=1024, max=262144,
          help="Nombre d'échantillons traités à chaque tour. Plus grand = moins "
               "de tours par seconde, donc moins de charge, mais plus de latence."),
        P("scene", "Scène", "select", "fm_band", options=SCENES),
        P("noise_db", "Niveau de bruit", "float", -75.0, unit="dB", min=-140, max=0),
    ]

    def setup(self):
        self.n = 0
        self.rng = np.random.default_rng(12345)

    def work(self, ins, ctx):
        sr = float(self.p["sample_rate"])
        n = int(self.p["block_size"])
        t = (self.n + np.arange(n)) / sr
        self.n += n

        amp = 10.0 ** (float(self.p["noise_db"]) / 20.0)
        x = (self.rng.normal(0, amp, n) + 1j * self.rng.normal(0, amp, n)).astype(C64)

        scene = self.p["scene"]
        if scene == "fm_band":
            # Trois « stations » à des décalages fixes, de puissances différentes.
            for off, a, dev in ((-300e3, 0.5, 40e3), (120e3, 0.25, 30e3), (380e3, 0.09, 25e3)):
                mod = np.cumsum(np.sin(2 * np.pi * (300 + abs(off) / 4e3) * t)) / sr
                x += a * np.exp(2j * np.pi * (off * t + dev * mod))
        elif scene == "two_tones":
            x += 0.5 * np.exp(2j * np.pi * -200e3 * t)
            x += 0.2 * np.exp(2j * np.pi * 310e3 * t)
        elif scene == "sweep":
            span, period = sr * 0.8, 6.0
            f = -span / 2 + span * ((t % period) / period)
            x += 0.4 * np.exp(2j * np.pi * np.cumsum(f) / sr)
        elif scene == "burst":
            on = ((t * 2.0).astype(int) % 3) == 0
            x += 0.45 * on * np.exp(2j * np.pi * 150e3 * t)
        return {"out": Signal(x, sr, float(self.p["center_freq"]))}


@register
class RtlSdrSource(Block):
    type = "rtlsdr_source"
    label = "RTL-SDR"
    category = "source"
    color = "amber"
    doc = ("Le vrai dongle, piloté directement par librtlsdr. Tant qu'il n'y en "
           "a pas, le bloc signale l'erreur et le graphe refuse de démarrer — "
           "c'est voulu, plutôt que de faire semblant.")
    outputs = [{"name": "out", "dtype": "iq"}]
    params = [
        P("sample_rate", "Cadence d'échantillonnage", "float", 2_400_000.0, unit="éch/s",
          bounds=(225_001, 3_200_000),
          help="⭐ 2 400 000 et pas 2 048 000 : c'est la seule des deux qui se "
               "divise JUSTE vers 48 kHz (÷50, ou ÷10 puis ÷5). Avec 2 048 000 "
               "il faudrait un rééchantillonneur à rapport fractionnaire."),
        P("center_freq", "Fréquence centrale", "freq", 100_000_000.0, unit="Hz",
          bounds=(24_000_000, 1_766_000_000),
          help="24 MHz à 1 766 MHz. Le dongle se réaccorde immédiatement."),
        P("gain", "Gain", "select", "auto", options=[{"value": "auto", "label": "auto"}] +
          [{"value": str(g), "label": f"{g} dB"} for g in (0, 9, 15, 20, 25, 30, 35, 40, 44, 49)]),
        P("ppm", "Correction PPM", "int", 0, min=-100, max=100,
          help="Ce dongle n'a pas de quartz compensé : il dérive. Se cale sur "
               "une station de fréquence connue."),
        P("block_size", "Taille de bloc", "int", 65536, min=1024, max=262144,
          help="65 536 à 2,4 MS/s = 27 ms par tour, 36 tours/s. Plus petit fait "
               "tourner la boucle pour rien ; plus grand ajoute de la latence."),
        P("device_index", "Index du périphérique", "int", 0, min=0, max=7),
    ]

    # Point d'entrée remplaçable par les tests (une fausse librtlsdr).
    open_dongle = staticmethod(rtl.Dongle)
    QUEUE = 6              # blocs d'avance tolérés avant de jeter les plus anciens

    def setup(self):
        self.dongle = None
        self.q: queue.Queue | None = None
        self.perdus = 0
        # Anneau de blocs PRÉALLOUÉS, remplis par le fil USB. Allouer un bloc
        # neuf à chaque tour (128 Kio, au-dessus du seuil où glibc passe par
        # mmap) coûtait des appels système et des défauts de page : mesuré,
        # +5 points de processeur « système » par rapport à pyrtlsdr. Taille :
        # la file pleine, plus le bloc en cours de traitement, plus celui en
        # cours de remplissage — aucun n'est réécrit tant qu'on s'en sert.
        nb = 2 * int(self.p["block_size"])          # 2 octets (I, Q) par échantillon
        self._ring = [np.empty(nb, dtype=np.uint8) for _ in range(self.QUEUE + 2)]
        self._ri = 0           # bloc de l'anneau en cours de remplissage
        self._fill = 0         # octets déjà écrits dans ce bloc
        sr = float(self.p["sample_rate"])
        if not any(lo <= sr <= hi for lo, hi in rtl.SAMPLE_RATE_RANGES):
            self.error = ("cadence impossible pour le dongle : 225 001 à 300 000 "
                          "ou 900 001 à 3 200 000 éch/s")
            return
        if not self.hardware:      # simple vérification du graphe
            return
        try:
            self.dongle = self.open_dongle(int(self.p["device_index"]))
            self.dongle.configure(int(sr), int(self.p["center_freq"]),
                                  int(self.p["ppm"]), self.p["gain"])
        except Exception as e:
            self.error = rtl.describe_error(e)
            self.close()
            return

        # ⚠️ La lecture USB tourne dans SON PROPRE FIL, et pas dans le tour de
        # graphe. Mesuré autrement : lire un bloc prend 27 ms (c'est sa durée
        # réelle), le traiter en prend 10 — enchaînés, le cycle fait 37 ms et on
        # ne tient plus les 36,6 tours/s du dongle. Le pilote comble en jetant
        # des échantillons, EN SILENCE, et ça s'entend en craquements réguliers.
        # Avec un fil dédié, l'acquisition du bloc suivant se fait PENDANT le
        # traitement du précédent : les deux se recouvrent au lieu de s'ajouter.
        self.q = queue.Queue(maxsize=self.QUEUE)
        self.dongle.start(self._on_bytes)

    def _on_bytes(self, chunk: bytes) -> None:
        """Appelé par le fil USB : recoupe le flux en blocs de `block_size`.

        Les tampons USB ont une taille fixe (64 Kio) sans rapport avec la
        taille de bloc choisie dans l'interface : on recopie dans l'anneau, et
        chaque bloc plein part dans la file.
        """
        src = np.frombuffer(chunk, dtype=np.uint8)
        i = 0
        while i < len(src):
            buf = self._ring[self._ri]
            k = min(len(buf) - self._fill, len(src) - i)
            buf[self._fill:self._fill + k] = src[i:i + k]
            self._fill += k
            i += k
            if self._fill == len(buf):
                self._push(buf)
                self._ri = (self._ri + 1) % len(self._ring)
                self._fill = 0

    def _push(self, raw: np.ndarray) -> None:
        if self.q.full():
            # File pleine = le traitement a décroché. On jette le PLUS ANCIEN
            # plutôt que le plus récent : sur une écoute en direct, accumuler
            # du retard est pire qu'un trou.
            try:
                self.q.get_nowait()
                self.perdus += 1
            except queue.Empty:
                pass
        try:
            self.q.put_nowait(raw)
        except queue.Full:
            pass

    def work(self, ins, ctx):
        if self.dongle is None or self.q is None:
            return {}
        try:
            raw = self.q.get(timeout=2.0)
        except queue.Empty:
            return {}
        return {"out": Signal(iq_from_u8(raw), float(self.p["sample_rate"]),
                              float(self.p["center_freq"]))}

    def close(self):
        """Rendre le dongle : annuler la lecture, attendre le fil, fermer.

        Tout l'ordre est dans `rtl.Dongle.close` ; ici on se contente de
        l'appeler une fois.
        """
        d, self.dongle = self.dongle, None
        if d is not None:
            d.close()


def iq_from_u8(raw) -> np.ndarray:
    """Octets bruts du dongle -> échantillons complex64 dans [-1, 1].

    ⚠️ On N'UTILISE PAS la conversion de pyrtlsdr (`read_samples()`, du temps où
    on s'en servait). Mesuré sur la machine hôte à 2,4 MS/s : la source SEULE
    saturait un cœur (100 %), la FFT n'ajoutant rien. Elle allouait un tableau
    **complex128** (16 octets par échantillon, soit 38 Mo/s ici) rempli par deux
    affectations entrelacées, puis divisé.

    Ici : une seule passe, et surtout la vue finale. Un tableau de float32 de
    longueur 2N relu en complex64 donne exactement N complexes, la partie
    réelle prise sur les indices pairs et l'imaginaire sur les impairs — c'est
    précisément la disposition I/Q du dongle. Zéro copie pour cette étape, et
    deux fois moins de mémoire que complex128.
    """
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    a -= 127.5          # le dongle code sur 8 bits non signés, centré sur 127,5
    a *= 1.0 / 127.5    # ramené dans [-1, 1]
    return a.view(C64)


# --------------------------------------------------------------------------
#  Traitement
# --------------------------------------------------------------------------

@register
class FreqShift(Block):
    type = "freq_shift"
    label = "Décalage de fréquence"
    category = "traitement"
    color = "sky"
    doc = ("Fait glisser tout le spectre pour amener au centre une station qui "
           "se trouvait sur le côté. C'est la première moitié de « écouter une "
           "station précise » ; la seconde est la décimation.")
    inputs = [{"name": "in", "dtype": "iq"}]
    outputs = [{"name": "out", "dtype": "iq"}]
    params = [P("offset_hz", "Décalage", "float", 0.0, unit="Hz",
                bounds=(-10_000_000, 10_000_000),
                help="Positif = on remonte la bande. La station qui était à "
                     "+120 kHz se retrouve au centre avec un décalage de 120 000.")]

    def setup(self):
        self.nco = Nco()

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None:
            return {}
        off = float(self.p["offset_hz"])
        return {"out": Signal(self.nco(s.data, off, s.sr), s.sr, s.fc + off)}


@register
class Decimate(Block):
    type = "decimate"
    label = "Décimation"
    category = "traitement"
    color = "violet"
    doc = ("FILTRER puis JETER des échantillons. Garder 1 échantillon sur N "
           "divise la cadence par N — donc le coût de tout ce qui suit. Mais "
           "sans le filtre passe-bas préalable, tout ce qui dépasse la nouvelle "
           "bande se REPLIE dedans et apparaît comme des stations fantômes. "
           "C'est pour ça que le filtre n'est pas optionnel.")
    inputs = [{"name": "in", "dtype": "iq"}]
    outputs = [{"name": "out", "dtype": "iq"}]
    params = [
        P("factor", "Facteur", "int", 4, min=1, max=256,
          help="1 échantillon gardé sur N. 2 400 000 ÷ 12 = 200 000, la largeur "
               "d'une station FM."),
        P("cutoff_ratio", "Coupure", "float", 0.45, min=0.05, max=0.49, step=0.01,
          help="Fraction de la NOUVELLE cadence. 0,45 laisse une petite marge de "
               "transition au filtre ; au-delà de 0,5 le repliement est garanti."),
        P("ntaps", "Longueur du filtre", "int", 63, min=7, max=511,
          help="Plus de coefficients = flanc plus raide, mais plus de calcul."),
    ]

    def setup(self):
        self.dec = None
        self._key = None

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None:
            return {}
        f = max(1, int(self.p["factor"]))
        out_sr = s.sr / f
        key = (s.sr, f, float(self.p["cutoff_ratio"]), int(self.p["ntaps"]))
        if key != self._key:  # reconstruit le filtre si un réglage a bougé
            self.dec = Decimator(out_sr * float(self.p["cutoff_ratio"]), s.sr, f,
                                 int(self.p["ntaps"]))
            self._key = key
        return {"out": Signal(self.dec(s.data), out_sr, s.fc)}


@register
class Fft(Block):
    type = "fft"
    label = "FFT"
    category = "traitement"
    color = "emerald"
    doc = ("Passe du temps à la fréquence : en entrée un flux d'échantillons, "
           "en sortie un spectre en dB. Le bloc est découpé en segments dont on "
           "moyenne les spectres, ce qui calme le bruit à l'affichage.")
    inputs = [{"name": "in", "dtype": "iq"}]
    outputs = [{"name": "out", "dtype": "spectrum"}]
    params = [
        P("size", "Taille", "select", 2048, options=[{"value": v, "label": str(v)}
                                                     for v in (256, 512, 1024, 2048, 4096, 8192)],
          help="Résolution du spectre : cadence ÷ taille. Plus grand = plus fin, "
               "mais plus lent à réagir."),
        P("window", "Fenêtre", "select", "hann",
          options=[{"value": w, "label": w} for w in ("hann", "hamming", "blackman", "rect")],
          help="Adoucit les bords du segment. Sans fenêtre (rect), une porteuse "
               "pure « bave » sur tout le spectre."),
        P("average", "Lissage", "float", 0.5, min=0.0, max=0.95, step=0.05,
          help="Moyenne glissante entre deux trames. 0 = brut et nerveux."),
    ]

    def setup(self):
        self.acc = None

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None:
            return {}
        spec = spectrum_db(s.data, int(self.p["size"]), str(self.p["window"]))
        a = float(self.p["average"])
        if self.acc is None or len(self.acc) != len(spec):
            self.acc = spec
        else:
            self.acc = (a * self.acc + (1.0 - a) * spec).astype(F32)
        return {"out": Signal(self.acc, s.sr, s.fc, kind="spectrum")}


# --------------------------------------------------------------------------
#  Afficheurs
# --------------------------------------------------------------------------

class _Sink(Block):
    """Base des afficheurs : limite la cadence d'émission vers le navigateur."""
    category = "afficheur"
    color = "rose"

    def setup(self):
        self.last = 0.0
        self.seq = 0

    def due(self, ctx) -> bool:
        fps = max(1.0, float(self.p.get("fps", 20)))
        if ctx.now - self.last < 1.0 / fps:
            return False
        self.last = ctx.now
        return True


@register
class SpectrumSink(_Sink):
    type = "spectrum_sink"
    label = "Spectre"
    inputs = [{"name": "in", "dtype": "spectrum"}]
    doc = "Trace le spectre instantané. Brancher après une FFT."
    params = [
        P("min_db", "Plancher", "float", -110.0, unit="dB", min=-160, max=0),
        P("max_db", "Plafond", "float", -10.0, unit="dB", min=-160, max=40),
        P("fps", "Images/s", "int", 20, min=1, max=60),
    ]

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or not self.due(ctx):
            return {}
        lo, hi = float(self.p["min_db"]), float(self.p["max_db"])
        q = np.clip((s.data - lo) / max(1e-6, hi - lo), 0, 1)
        self.seq += 1
        ctx.emit({"sink": self.id, "kind": "spectrum", "n": int(len(q)),
                  "sr": s.sr, "fc": s.fc, "min_db": lo, "max_db": hi, "seq": self.seq},
                 (q * 255).astype(np.uint8).tobytes())
        return {}


@register
class WaterfallSink(_Sink):
    type = "waterfall_sink"
    label = "Waterfall"
    inputs = [{"name": "in", "dtype": "spectrum"}]
    doc = ("Empile les spectres dans le temps : une ligne par trame, l'image "
           "défile. C'est la vue qui rend visible ce qu'on n'entend pas.")
    params = [
        P("min_db", "Plancher", "float", -110.0, unit="dB", min=-160, max=0),
        P("max_db", "Plafond", "float", -10.0, unit="dB", min=-160, max=40),
        P("fps", "Lignes/s", "int", 25, min=1, max=60),
        P("palette", "Palette", "select", "turbo",
          options=[{"value": v, "label": v} for v in ("turbo", "viridis", "gris", "braise")]),
    ]

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or not self.due(ctx):
            return {}
        lo, hi = float(self.p["min_db"]), float(self.p["max_db"])
        q = np.clip((s.data - lo) / max(1e-6, hi - lo), 0, 1)
        self.seq += 1
        ctx.emit({"sink": self.id, "kind": "waterfall", "n": int(len(q)),
                  "sr": s.sr, "fc": s.fc, "min_db": lo, "max_db": hi,
                  "palette": self.p["palette"], "seq": self.seq},
                 (q * 255).astype(np.uint8).tobytes())
        return {}


@register
class ScopeSink(_Sink):
    type = "scope_sink"
    label = "Oscilloscope"
    inputs = [{"name": "in", "dtype": "iq"}]
    doc = "Le signal dans le temps : composantes I et Q. Utile pour voir la forme brute."
    params = [
        P("points", "Points", "int", 512, min=64, max=4096),
        P("fps", "Images/s", "int", 15, min=1, max=60),
    ]

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or not self.due(ctx):
            return {}
        n = min(int(self.p["points"]), len(s.data))
        seg = s.data[:n]
        buf = np.empty(n * 2, dtype=np.float32)
        buf[0::2] = seg.real
        buf[1::2] = seg.imag
        self.seq += 1
        ctx.emit({"sink": self.id, "kind": "scope", "n": n, "sr": s.sr, "fc": s.fc,
                  "seq": self.seq}, buf.tobytes())
        return {}


@register
class PowerSink(_Sink):
    type = "power_sink"
    label = "Mesure de puissance"
    inputs = [{"name": "in", "dtype": "iq"}]
    doc = "Niveau moyen du signal, en dB. L'équivalent d'un S-mètre."
    params = [P("fps", "Mesures/s", "int", 10, min=1, max=30)]

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or not self.due(ctx):
            return {}
        p = float(10.0 * np.log10(np.mean(np.abs(s.data) ** 2) + 1e-20))
        self.seq += 1
        ctx.emit({"sink": self.id, "kind": "power", "db": round(p, 2),
                  "sr": s.sr, "fc": s.fc, "seq": self.seq}, b"")
        return {}


def catalogue() -> list[dict]:
    return [c.describe() for c in REGISTRY.values()]

# --------------------------------------------------------------------------
#  Démodulation — du signal radio vers du son
# --------------------------------------------------------------------------

@register
class FmDemod(Block):
    type = "fm_demod"
    label = "Démodulateur FM"
    category = "traitement"
    color = "sky"
    doc = ("Transforme un flux I/Q en audio. Deux modes : FM LARGE pour la radio "
           "commerciale (canal de 200 kHz), FM ÉTROITE pour les talkies et les "
           "relais (12,5 kHz). Sortie à 48 kHz, prête pour la carte son.")
    inputs = [{"name": "in", "dtype": "iq"}]
    outputs = [{"name": "out", "dtype": "audio"}]
    params = [
        P("mode", "Mode", "select", "wfm", options=[
            {"value": "wfm", "label": "FM large (radio)"},
            {"value": "nfm", "label": "FM étroite (talkies)"}]),
        P("audio_rate", "Cadence audio", "select", 48000, options=[
            {"value": 48000, "label": "48 000"}, {"value": 32000, "label": "32 000"},
            {"value": 24000, "label": "24 000"}],
          help="48 000 si la source est à 2 400 000 : toutes les divisions "
               "tombent juste. 32 000 va mieux avec 2 048 000."),
        P("channel_bw", "Largeur de canal", "float", 12500.0, unit="Hz",
          min=5000, max=50000,
          help="FM étroite seulement : filtre de canal appliqué AVANT de "
               "démoduler. Trop large, on entend les canaux voisins."),
        P("deemphasis", "Désaccentuation", "select", "50",
          options=[{"value": "50", "label": "50 µs (Europe)"},
                   {"value": "75", "label": "75 µs (Amérique)"},
                   {"value": "0", "label": "aucune"}],
          help="Sans elle, le son est criard et sifflant. C'est le défaut le "
               "plus reconnaissable d'un démodulateur FM maison."),
        P("gain", "Volume", "float", 1.0, min=0.0, max=8.0, step=0.1),
    ]

    def setup(self):
        self._key = None

    def _chain(self, sr):
        """(Re)construit la chaîne quand un réglage ou la cadence d'entrée change.

        Deux étages de décimation, jamais un seul : passer de 2,4 MS/s à 48 kHz
        d'un coup demanderait un filtre à des centaines de coefficients, parce
        que sa bande de transition serait minuscule devant la cadence d'entrée.
        En deux temps — grossier puis étroit — chaque filtre reste court.
        """
        mode = str(self.p["mode"])
        ar = float(self.p["audio_rate"])
        d1 = max(1, int(round(sr / 240_000.0)))      # vers une FI d'environ 240 kHz
        if1 = sr / d1
        self.decA = Decimator(min(100_000.0, if1 * 0.45), sr, d1, 63)
        self.d2 = max(1, int(round(if1 / ar)))
        self.out_sr = if1 / self.d2
        self.disc = FmDiscriminator()
        if mode == "nfm":
            # Filtre de canal AVANT la démodulation : en FM étroite, tout ce qui
            # dépasse le canal se retrouverait mélangé dans l'audio.
            self.decB = Decimator(float(self.p["channel_bw"]) / 2, if1, self.d2, 95)
            self.decC = None
        else:
            # En FM large, on démodule à la FI puis on filtre l'audio à 15 kHz
            # (la bande utile d'une station) avant de décimer.
            self.decB = None
            self.decC = Decimator(15_000.0, if1, self.d2, 63)
        tau = float(self.p["deemphasis"])
        self.deemph = Deemphasis(tau * 1e-6, self.out_sr) if tau > 0 else None

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or len(s.data) == 0:
            return {}
        key = (s.sr, self.p["mode"], self.p["audio_rate"], self.p["channel_bw"],
               self.p["deemphasis"])
        if key != self._key:
            self._chain(s.sr)
            self._key = key

        y = self.decA(s.data)
        if self.decB is not None:                   # FM étroite
            a = self.disc(self.decB(y))
        else:                                       # FM large
            a = self.decC(self.disc(y))
        if self.deemph is not None:
            a = self.deemph(a)
        a = (a * float(self.p["gain"])).astype(F32)
        return {"out": Signal(a, self.out_sr, s.fc, kind="audio")}


@register
class AmDemod(Block):
    type = "am_demod"
    label = "Démodulateur AM"
    category = "traitement"
    color = "sky"
    doc = ("Pour l'aviation. L'information est dans l'AMPLITUDE : on prend le "
           "module du signal complexe, on retire la porteuse (une constante), "
           "et c'est fait.")
    inputs = [{"name": "in", "dtype": "iq"}]
    outputs = [{"name": "out", "dtype": "audio"}]
    params = [
        P("audio_rate", "Cadence audio", "select", 48000, options=[
            {"value": 48000, "label": "48 000"}, {"value": 32000, "label": "32 000"}]),
        P("channel_bw", "Largeur de canal", "float", 8000.0, unit="Hz",
          min=2000, max=20000,
          help="Un canal aviation fait environ 8 kHz. Filtré avant démodulation."),
        P("gain", "Volume", "float", 2.0, min=0.0, max=16.0, step=0.1),
    ]

    def setup(self):
        self._key = None

    def _chain(self, sr):
        ar = float(self.p["audio_rate"])
        d1 = max(1, int(round(sr / 240_000.0)))
        if1 = sr / d1
        self.decA = Decimator(min(100_000.0, if1 * 0.45), sr, d1, 63)
        self.d2 = max(1, int(round(if1 / ar)))
        self.out_sr = if1 / self.d2
        self.decB = Decimator(float(self.p["channel_bw"]) / 2, if1, self.d2, 95)
        # Coupure très basse : on retire la porteuse sans toucher à la voix.
        self.dc = DcBlock(20.0, self.out_sr)

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or len(s.data) == 0:
            return {}
        key = (s.sr, self.p["audio_rate"], self.p["channel_bw"])
        if key != self._key:
            self._chain(s.sr)
            self._key = key
        y = self.decB(self.decA(s.data))
        a = self.dc(np.abs(y).astype(F32)) * float(self.p["gain"])
        return {"out": Signal(a.astype(F32), self.out_sr, s.fc, kind="audio")}


@register
class AudioSink(_Sink):
    type = "audio_sink"
    label = "Écoute"
    inputs = [{"name": "in", "dtype": "audio"}]
    doc = ("Envoie le son au navigateur. Il faut cliquer sur « Écouter » dans le "
           "panneau : les navigateurs refusent de jouer du son sans un geste de "
           "l'utilisateur, et ne préviennent pas quand ils bloquent.")
    params = [
        P("volume", "Volume", "float", 1.0, min=0.0, max=4.0, step=0.05),
        P("mute", "Silence", "bool", False),
    ]

    def setup(self):
        super().setup()

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None or len(s.data) == 0:
            return {}
        # Pas de limitation de cadence ici, contrairement aux afficheurs : une
        # image de spectre sautée ne se voit pas, un morceau d'audio manquant
        # s'entend. Tout ce qui est produit doit partir.
        a = np.zeros_like(s.data) if self.p["mute"] else s.data
        a = a * float(self.p["volume"])
        n_clip = int(np.count_nonzero(np.abs(a) > 1.0))
        pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype(np.int16)
        self.seq += 1
        ctx.emit({"sink": self.id, "kind": "audio", "n": int(len(pcm)),
                  "sr": s.sr, "fc": s.fc, "seq": self.seq, "clip": n_clip},
                 pcm.tobytes())
        return {}
