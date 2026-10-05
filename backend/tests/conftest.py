"""Outils partagés : une FAUSSE librtlsdr, pour tester sans dongle branché."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from app import rtl
from app.blocks import RtlSdrSource


class FakeLib:
    """Imite `rtl.Lib` : mêmes méthodes, mêmes codes de retour.

    Le flux produit une rampe d'octets (0, 1, 2, … 255, 0, …) : on peut ainsi
    vérifier qu'aucun octet n'est perdu ni dupliqué au découpage en blocs.
    """

    GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254,
             280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496]

    def __init__(self, open_error: int | None = None, stuck: bool = False,
                 chunk_len: int | None = None, max_chunks: int | None = None):
        self.open_error = open_error
        self.chunk_len = chunk_len    # None = la taille demandée par Dongle
        self.max_chunks = max_chunks  # None = flux sans fin
        self.stuck = stuck            # lecture qui ignore l'annulation
        self.calls: list[tuple] = []
        self.cancelled = threading.Event()
        self.opened = 0
        self.closed = 0
        self._next = 0

    def device_count(self):
        return 1

    def open(self, index):
        if self.open_error is not None:
            raise rtl.RtlError(self.open_error, "ouverture")
        self.opened += 1
        self.cancelled = threading.Event()   # nouvelle session de lecture
        return object()

    def close(self, h):
        self.closed += 1

    def _rec(self, *a):
        self.calls.append(a)
        return 0

    def set_sample_rate(self, h, rate): return self._rec("sr", rate)
    def set_center_freq(self, h, f): return self._rec("fc", f)
    def set_freq_correction(self, h, ppm):
        self._rec("ppm", ppm)
        return -2 if ppm == 0 else 0           # comme librtlsdr : « déjà réglé »
    def tuner_gains(self, h): return list(self.GAINS)
    def set_gain_mode(self, h, manual): return self._rec("mode", manual)
    def set_gain(self, h, g): return self._rec("gain", g)
    def reset_buffer(self, h): return self._rec("reset")

    def read_async(self, h, on_bytes, buf_num, buf_len):
        n = self.chunk_len or buf_len
        sent = 0
        while not self.cancelled.is_set() or self.stuck:
            if self.max_chunks is None or sent < self.max_chunks:
                chunk = (np.arange(self._next, self._next + n) % 256).astype(np.uint8)
                self._next += n
                sent += 1
                on_bytes(chunk.tobytes())
            self.cancelled.wait(0.002)
            if self.stuck and self.cancelled.is_set():
                threading.Event().wait(5)     # « bloqué » dans l'USB
                return -1
        return 0

    def cancel_async(self, h):
        self.cancelled.set()
        return 0


@pytest.fixture
def fake_lib(monkeypatch):
    """Branche la fausse librtlsdr sous le bloc RTL-SDR."""
    lib = FakeLib()
    monkeypatch.setattr(RtlSdrSource, "open_dongle",
                        staticmethod(lambda index: rtl.Dongle(index, lib=lib)))
    return lib
