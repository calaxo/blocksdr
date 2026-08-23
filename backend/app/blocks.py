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

import time
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from .dsp import C64, F32, Fir, Nco, lowpass_taps, spectrum_db


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
    """Raccourci pour décrire un paramètre (le front en fait un champ)."""
    d = {"name": name, "label": label, "type": ptype, "default": default}
    d.update(kw)
    return d


class Block:
    type = "block"
    label = "Bloc"
    category = "divers"
    color = "slate"
    doc = ""
    inputs: list[dict] = []
    outputs: list[dict] = []
    params: list[dict] = []

    def __init__(self, node_id: str, params: dict[str, Any]):
        self.id = node_id
        self.p = {d["name"]: d["default"] for d in self.params}
        self.p.update({k: v for k, v in (params or {}).items() if k in self.p})
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
          help="Largeur de la fenêtre observée. Un RTL-SDR réel monte à 2 400 000."),
        P("center_freq", "Fréquence centrale", "freq", 100_000_000.0, unit="Hz"),
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
    doc = ("Le vrai dongle. Nécessite `pyrtlsdr` et un RTL-SDR accessible. "
           "Tant qu'il n'y en a pas, le bloc signale l'erreur et le graphe "
           "refuse de démarrer — c'est voulu, plutôt que de faire semblant.")
    outputs = [{"name": "out", "dtype": "iq"}]
    params = [
        P("sample_rate", "Cadence d'échantillonnage", "float", 2_048_000.0, unit="éch/s",
          help="Les deux valeurs sûres du RTL2832U sont 2 048 000 et 2 400 000."),
        P("center_freq", "Fréquence centrale", "freq", 100_000_000.0, unit="Hz",
          help="24 MHz à 1 766 MHz. Le dongle se réaccorde immédiatement."),
        P("gain", "Gain", "select", "auto", options=[{"value": "auto", "label": "auto"}] +
          [{"value": str(g), "label": f"{g} dB"} for g in (0, 9, 15, 20, 25, 30, 35, 40, 44, 49)]),
        P("ppm", "Correction PPM", "int", 0, min=-100, max=100,
          help="Ce dongle n'a pas de quartz compensé : il dérive. Se cale sur "
               "une station de fréquence connue."),
        P("block_size", "Taille de bloc", "int", 16384, min=1024, max=262144),
        P("device_index", "Index du périphérique", "int", 0, min=0, max=7),
    ]

    def setup(self):
        self.sdr = None
        try:
            from rtlsdr import RtlSdr  # type: ignore
        except Exception as e:  # pragma: no cover - dépend de l'environnement
            self.error = (f"pyrtlsdr absent de l'image ({e}). "
                          "Vérifie que requirements.txt le contient et que la CI a "
                          "reconstruit l'image. En attendant : source simulée.")
            return
        try:
            self.sdr = RtlSdr(device_index=int(self.p["device_index"]))
            self.sdr.sample_rate = float(self.p["sample_rate"])
            self.sdr.center_freq = float(self.p["center_freq"])
            self.sdr.freq_correction = int(self.p["ppm"]) or 1
            g = self.p["gain"]
            self.sdr.gain = "auto" if g == "auto" else float(g)
        except Exception as e:
            msg = str(e)
            # Les deux échecs qu'on rencontre vraiment, et leur remède.
            if "usb_open" in msg or "No device" in msg.lower() or "-1" in msg:
                self.error = ("Aucun dongle visible. Le conteneur reçoit-il "
                              "/dev/bus/usb ? (bloc `devices` du docker-compose)")
            elif "busy" in msg.lower() or "claim" in msg.lower() or "-6" in msg:
                self.error = ("Dongle déjà pris par un autre programme — "
                              "openwebrx, très probablement. UN DONGLE = UN "
                              "PROGRAMME : arrête l'autre stack d'abord.")
            else:
                self.error = f"Dongle inaccessible : {msg}"
            self.sdr = None

    def work(self, ins, ctx):
        if self.sdr is None:
            return {}
        n = int(self.p["block_size"])

        # ⚠️ On N'UTILISE PAS `read_samples()` de pyrtlsdr. Mesuré sur cette VM
        # à 2,4 MS/s : la source SEULE saturait un cœur (100 %), la FFT
        # n'ajoutant rien. En cause, sa conversion interne : elle alloue un
        # tableau **complex128** (16 octets par échantillon, soit 38 Mo/s ici) et
        # le remplit par deux affectations entrelacées, puis divise.
        #
        # Ici : une seule passe, et surtout la vue finale. Un tableau de float32
        # de longueur 2N relu en complex64 donne exactement N complexes, la
        # partie réelle prise sur les indices pairs et l'imaginaire sur les
        # impairs — c'est précisément la disposition I/Q du dongle. Zéro copie
        # pour cette étape, et deux fois moins de mémoire que complex128.
        raw = self.sdr.read_bytes(2 * n)
        a = np.frombuffer(memoryview(raw), dtype=np.uint8).astype(np.float32)
        a -= 127.5          # le dongle code sur 8 bits non signés, centré sur 127,5
        a *= 1.0 / 127.5    # ramené dans [-1, 1]
        x = a.view(C64)
        return {"out": Signal(x, float(self.p["sample_rate"]), float(self.p["center_freq"]))}

    def close(self):
        if self.sdr is not None:
            try:
                self.sdr.close()
            except Exception:
                pass


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
        self.fir = None
        self._key = None

    def work(self, ins, ctx):
        s = ins.get("in")
        if s is None:
            return {}
        f = max(1, int(self.p["factor"]))
        out_sr = s.sr / f
        key = (s.sr, f, float(self.p["cutoff_ratio"]), int(self.p["ntaps"]))
        if key != self._key:  # reconstruit le filtre si un réglage a bougé
            self.fir = Fir(lowpass_taps(out_sr * float(self.p["cutoff_ratio"]), s.sr,
                                        int(self.p["ntaps"])))
            self._key = key
        y = self.fir(s.data)
        if f > 1:
            y = y[::f]
        return {"out": Signal(y, out_sr, s.fc)}


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
