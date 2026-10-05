"""Accès direct au dongle RTL-SDR, par `ctypes` vers la librtlsdr du système.

Pourquoi pas pyrtlsdr : on n'utilisait qu'une poignée de ses fonctions, et il
apportait avec lui un serveur TCP, une couche asyncio, une recherche de la
bibliothèque jusque dans des chemins RELATIFS au dossier courant
(`../librtlsdr.so`), et — jusqu'à la 0.3.0 — une dépendance à `pkg_resources`
qui obligeait à brider setuptools. Ici : une quinzaine de fonctions C, toutes
listées plus bas, et rien d'autre.

Le module est découpé en deux couches, pour pouvoir le tester sans matériel :

  - `Lib` : la traduction mot à mot des fonctions C. Rien d'autre.
  - `Dongle` : ce que le reste du code manipule (réglages, flux, fermeture).
    Il reçoit sa `Lib` en paramètre ; les tests lui en donnent une fausse.

⚠️ La lecture se fait en mode ASYNCHRONE (`rtlsdr_read_async`), et ce n'est pas
un détail. C'est la seule façon prévue par librtlsdr d'arrêter proprement une
lecture depuis un autre fil : `rtlsdr_cancel_async`. Avec la lecture
synchrone, il fallait fermer le périphérique SOUS les pieds du fil qui lisait
encore — ça marchait, mais en libérant une structure C en cours d'utilisation.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from typing import Callable

# Codes d'erreur libusb qu'on rencontre vraiment à l'ouverture.
ERR_IO = -1            # rtlsdr_open : aucun périphérique à cet index
ERR_ACCESS = -3        # droits insuffisants sur /dev/bus/usb/...
ERR_BUSY = -6          # interface déjà réclamée par un autre programme

# Valeurs acceptées par librtlsdr pour la cadence (deux plages, avec un trou).
SAMPLE_RATE_RANGES = ((225_001, 300_000), (900_001, 3_200_000))

# Tampons USB de la lecture asynchrone. La longueur doit être un multiple de
# 512 (taille d'un paquet USB). 64 Kio = 13,6 ms à 2,4 MS/s : environ 70 appels
# par seconde vers Python, ce qui ne coûte rien, et 8 tampons donnent ~110 ms
# de marge au noyau si Python prend du retard.
USB_BUF_LEN = 65_536
USB_BUF_NUM = 8


class RtlError(Exception):
    """Échec d'un appel à librtlsdr. `code` est le code de retour C."""

    def __init__(self, code: int, what: str):
        super().__init__(f"{what} (code {code})")
        self.code = code


# --------------------------------------------------------------------------
#  Couche 1 : les fonctions C, telles quelles
# --------------------------------------------------------------------------

_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_ubyte),
                             ctypes.c_uint32, ctypes.c_void_p)


def _load_library() -> ctypes.CDLL:
    """Charge librtlsdr par son NOM, jamais par un chemin relatif.

    Sans barre oblique, le chargeur dynamique ne cherche que dans les
    répertoires système : impossible de lui faire charger un fichier déposé
    dans le dossier courant.
    """
    for name in ("librtlsdr.so.0", ctypes.util.find_library("rtlsdr")):
        if not name:
            continue
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise OSError("librtlsdr introuvable (paquet librtlsdr0 absent de l'image ?)")


class Lib:
    """Traduction mot à mot de l'API C de librtlsdr (rtl-sdr.h)."""

    def __init__(self) -> None:
        c = _load_library()
        dev = ctypes.c_void_p
        sig = {
            # nom C                   : (type de retour, types des arguments)
            "rtlsdr_get_device_count": (ctypes.c_uint32, []),
            "rtlsdr_open": (ctypes.c_int, [ctypes.POINTER(dev), ctypes.c_uint32]),
            "rtlsdr_close": (ctypes.c_int, [dev]),
            "rtlsdr_set_sample_rate": (ctypes.c_int, [dev, ctypes.c_uint32]),
            "rtlsdr_set_center_freq": (ctypes.c_int, [dev, ctypes.c_uint32]),
            "rtlsdr_set_freq_correction": (ctypes.c_int, [dev, ctypes.c_int]),
            "rtlsdr_get_tuner_gains": (ctypes.c_int, [dev, ctypes.POINTER(ctypes.c_int)]),
            "rtlsdr_set_tuner_gain_mode": (ctypes.c_int, [dev, ctypes.c_int]),
            "rtlsdr_set_tuner_gain": (ctypes.c_int, [dev, ctypes.c_int]),
            "rtlsdr_reset_buffer": (ctypes.c_int, [dev]),
            "rtlsdr_read_async": (ctypes.c_int, [dev, _CALLBACK, ctypes.c_void_p,
                                                 ctypes.c_uint32, ctypes.c_uint32]),
            "rtlsdr_cancel_async": (ctypes.c_int, [dev]),
        }
        for name, (res, args) in sig.items():
            f = getattr(c, name)
            f.restype, f.argtypes = res, args
        self.c = c

    def device_count(self) -> int:
        return int(self.c.rtlsdr_get_device_count())

    def open(self, index: int) -> ctypes.c_void_p:
        h = ctypes.c_void_p()
        r = self.c.rtlsdr_open(ctypes.byref(h), index)
        if r < 0:
            raise RtlError(r, f"ouverture du périphérique {index}")
        return h

    def close(self, h) -> None:
        self.c.rtlsdr_close(h)

    def set_sample_rate(self, h, rate: int) -> int:
        return self.c.rtlsdr_set_sample_rate(h, rate)

    def set_center_freq(self, h, freq: int) -> int:
        return self.c.rtlsdr_set_center_freq(h, freq)

    def set_freq_correction(self, h, ppm: int) -> int:
        return self.c.rtlsdr_set_freq_correction(h, ppm)

    def tuner_gains(self, h) -> list[int]:
        """Gains possibles du tuner, en DIXIÈMES de dB (convention de librtlsdr)."""
        n = self.c.rtlsdr_get_tuner_gains(h, None)
        if n <= 0:
            return []
        buf = (ctypes.c_int * n)()
        n = self.c.rtlsdr_get_tuner_gains(h, buf)
        return list(buf[:max(0, n)])

    def set_gain_mode(self, h, manual: bool) -> int:
        return self.c.rtlsdr_set_tuner_gain_mode(h, int(manual))

    def set_gain(self, h, tenth_db: int) -> int:
        return self.c.rtlsdr_set_tuner_gain(h, tenth_db)

    def reset_buffer(self, h) -> int:
        return self.c.rtlsdr_reset_buffer(h)

    def read_async(self, h, on_bytes: Callable[[bytes], None],
                   buf_num: int, buf_len: int) -> int:
        """BLOQUE jusqu'à `cancel_async`, en appelant `on_bytes` à chaque tampon.

        Le tampon C est réutilisé par librtlsdr dès le retour du rappel : on en
        fait donc une COPIE (`string_at`), sans quoi les blocs mis en file
        seraient réécrits pendant qu'ils attendent d'être traités.
        """
        def trampoline(buf, length, _ctx):
            on_bytes(ctypes.string_at(buf, length))

        cb = _CALLBACK(trampoline)     # référence gardée pendant tout l'appel
        return self.c.rtlsdr_read_async(h, cb, None, buf_num, buf_len)

    def cancel_async(self, h) -> int:
        return self.c.rtlsdr_cancel_async(h)


# --------------------------------------------------------------------------
#  Couche 2 : le dongle tel que le voit BlockSDR
# --------------------------------------------------------------------------

def nearest_gain(wanted_db: float, gains_tenth_db: list[int]) -> int:
    """Gain réellement disponible le plus proche de celui demandé (en 1/10 dB).

    Le tuner n'accepte qu'une liste fixe de valeurs ; demander 30 dB à un R820T
    donne en fait 29,7 dB.
    """
    if not gains_tenth_db:
        raise ValueError("le tuner ne déclare aucun gain")
    return min(gains_tenth_db, key=lambda g: abs(g - wanted_db * 10.0))


class Dongle:
    """Un RTL-SDR ouvert : réglages, flux d'octets I/Q, fermeture.

    Le flux tourne dans un fil dédié (`start`) qui appelle `on_bytes` avec des
    morceaux d'octets bruts. `close` peut être appelé depuis n'importe quel fil.
    """

    def __init__(self, index: int = 0, lib: Lib | None = None):
        self.lib = lib or Lib()
        self.h = self.lib.open(index)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()     # protège la fermeture (une seule fois)
        self._closed = False

    # -- réglages ----------------------------------------------------------
    def _check(self, r: int, what: str) -> None:
        if r < 0:
            raise RtlError(r, what)

    def configure(self, sample_rate: int, center_freq: int, ppm: int,
                  gain: str | float) -> None:
        """Applique les réglages, dans l'ordre que recommande librtlsdr."""
        self._check(self.lib.set_sample_rate(self.h, int(sample_rate)),
                    f"cadence {int(sample_rate)} éch/s refusée")
        self._check(self.lib.set_center_freq(self.h, int(center_freq)),
                    f"fréquence {int(center_freq)} Hz refusée")
        r = self.lib.set_freq_correction(self.h, int(ppm))
        # -2 = « déjà réglé à cette valeur » : librtlsdr le signale comme une
        # erreur alors que c'est exactement ce qu'on voulait.
        if r not in (0, -2):
            self._check(r, f"correction {ppm} ppm refusée")
        if gain == "auto":
            self._check(self.lib.set_gain_mode(self.h, False), "gain automatique refusé")
        else:
            g = nearest_gain(float(gain), self.lib.tuner_gains(self.h))
            self._check(self.lib.set_gain_mode(self.h, True), "gain manuel refusé")
            self._check(self.lib.set_gain(self.h, g), f"gain {g / 10} dB refusé")
        # Vide le FIFO du démodulateur : sans ça, les premiers octets lus sont
        # des restes de la session précédente.
        self._check(self.lib.reset_buffer(self.h), "remise à zéro du tampon")

    # -- flux --------------------------------------------------------------
    def start(self, on_bytes: Callable[[bytes], None]) -> None:
        """Lance la lecture continue dans un fil dédié."""
        def boucle():
            def rappel(chunk: bytes):
                if self._stop.is_set():
                    # Rattrape un `close` arrivé AVANT que la lecture ait
                    # démarré : librtlsdr ignore une annulation demandée trop
                    # tôt, mais on peut la redemander d'ici, à coup sûr.
                    self.lib.cancel_async(self.h)
                    return
                try:
                    on_bytes(chunk)
                except Exception:      # une exception ne doit pas traverser le C
                    self._stop.set()
                    self.lib.cancel_async(self.h)

            if not self._stop.is_set():
                self.lib.read_async(self.h, rappel, USB_BUF_NUM, USB_BUF_LEN)

        self._thread = threading.Thread(target=boucle, name="rtlsdr", daemon=True)
        self._thread.start()

    def close(self, timeout: float = 3.0) -> bool:
        """Arrête le flux PUIS rend le périphérique. Rend False si le fil traîne.

        L'ordre est celui que prévoit librtlsdr : annuler la lecture, attendre
        que le fil en sorte, et seulement alors fermer. On redemande
        l'annulation tant que le fil tourne, pour couvrir le cas où elle est
        arrivée avant que la lecture ne soit vraiment lancée.

        Si le fil ne sort pas à temps, on NE ferme PAS : libérer la structure C
        sous un fil qui s'en sert encore serait pire qu'un dongle gardé.
        """
        with self._lock:
            if self._closed:
                return True
            self._stop.set()
            t = self._thread
            if t is not None:
                waited = 0.0
                while t.is_alive() and waited < timeout:
                    self.lib.cancel_async(self.h)
                    t.join(0.1)
                    waited += 0.1
                if t.is_alive():
                    return False
            self.lib.close(self.h)
            self._closed = True
            return True


def describe_error(e: Exception) -> str:
    """Message pour l'interface, avec le remède des échecs qu'on rencontre vraiment."""
    code = getattr(e, "code", None)
    if code == ERR_BUSY:
        return ("Dongle déjà pris. UN DONGLE = UN PROGRAMME : soit un autre "
                "conteneur l'utilise, soit le graphe précédent ne l'a pas rendu. "
                "Dans le doute, redémarrer le conteneur le libère.")
    if code == ERR_ACCESS:
        return ("Accès au dongle refusé : le conteneur ne tourne pas en root, il "
                "lui faut le groupe propriétaire de /dev/bus/usb (group_add du "
                "docker-compose).")
    if code == ERR_IO:
        return ("Aucun dongle visible. Le conteneur reçoit-il /dev/bus/usb ? "
                "(bloc `devices` du docker-compose)")
    if isinstance(e, OSError):
        return f"librtlsdr inutilisable : {e}"
    return f"Dongle inaccessible : {e}"
