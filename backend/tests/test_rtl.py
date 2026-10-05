"""Le client du dongle, contre une fausse librtlsdr (aucun matériel requis)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from app import rtl
from app.blocks import Ctx, RtlSdrSource, iq_from_u8
from tests.conftest import FakeLib


def test_conversion_octets_vers_iq():
    # 0 -> -1, 255 -> +1, et l'ordre I, Q, I, Q… du dongle.
    x = iq_from_u8(bytes([0, 255, 255, 0]))
    assert x.dtype == np.complex64
    np.testing.assert_allclose(x, [-1 + 1j, 1 - 1j], atol=1e-6)


def test_gain_le_plus_proche():
    assert rtl.nearest_gain(30, FakeLib.GAINS) == 297      # 29,7 dB
    assert rtl.nearest_gain(0, FakeLib.GAINS) == 0
    assert rtl.nearest_gain(100, FakeLib.GAINS) == 496
    with pytest.raises(ValueError):
        rtl.nearest_gain(10, [])


def test_reglages_dans_l_ordre():
    lib = FakeLib()
    d = rtl.Dongle(0, lib=lib)
    d.configure(2_400_000, 100_000_000, 0, "30")
    assert lib.calls == [("sr", 2_400_000), ("fc", 100_000_000), ("ppm", 0),
                         ("mode", True), ("gain", 297), ("reset",)]
    lib.calls.clear()
    d.configure(2_400_000, 100_000_000, 5, "auto")
    assert ("mode", False) in lib.calls and not any(c[0] == "gain" for c in lib.calls)
    d.close()


def test_reglage_refuse_leve_une_erreur():
    lib = FakeLib()
    lib.set_center_freq = lambda h, f: -22
    d = rtl.Dongle(0, lib=lib)
    with pytest.raises(rtl.RtlError):
        d.configure(2_400_000, 5, 0, "auto")
    d.close()


@pytest.mark.parametrize("code, mot", [(rtl.ERR_BUSY, "déjà pris"),
                                       (rtl.ERR_ACCESS, "refusé"),
                                       (rtl.ERR_IO, "Aucun dongle")])
def test_messages_d_erreur(code, mot):
    assert mot in rtl.describe_error(rtl.RtlError(code, "x"))


def test_fermeture_annule_puis_ferme():
    lib = FakeLib()
    d = rtl.Dongle(0, lib=lib)
    recus = []
    d.start(recus.append)
    time.sleep(0.05)
    assert d.close() is True
    assert lib.cancelled.is_set() and lib.closed == 1
    assert d.close() is True and lib.closed == 1          # une seule fois
    assert recus


def test_fil_bloque_on_ne_ferme_pas():
    """Si la lecture ne rend pas la main, on NE libère PAS la structure C."""
    lib = FakeLib(stuck=True)
    d = rtl.Dongle(0, lib=lib)
    d.start(lambda b: None)
    time.sleep(0.02)
    assert d.close(timeout=0.3) is False
    assert lib.closed == 0


def test_bloc_rtlsdr_decoupe_sans_perte(fake_lib):
    """Des tampons USB de taille quelconque sont recoupés en blocs EXACTS.

    3 tampons de 3 000 octets = 9 000 octets = 4 blocs de 1 024 échantillons
    (2 048 octets), et un reste qui attend la suite.
    """
    fake_lib.chunk_len, fake_lib.max_chunks = 3000, 3
    blk = RtlSdrSource("src", {"block_size": 1024, "sample_rate": 2_400_000})
    assert blk.error is None
    try:
        outs = [blk.work({}, Ctx(emit=lambda h, p: None))["out"] for _ in range(4)]
    finally:
        blk.close()
    assert fake_lib.closed == 1 and blk.perdus == 0
    assert all(len(s.data) == 1024 and s.sr == 2_400_000 for s in outs)
    # La rampe d'octets doit se poursuivre d'un bloc à l'autre : rien de perdu.
    flux = np.concatenate([s.data for s in outs])
    octets = np.empty(flux.size * 2, dtype=np.float32)
    octets[0::2], octets[1::2] = flux.real, flux.imag
    octets = np.round(octets * 127.5 + 127.5).astype(int)
    np.testing.assert_array_equal(octets, np.arange(octets.size) % 256)


def test_bloc_rtlsdr_sans_materiel_n_ouvre_rien(fake_lib):
    blk = RtlSdrSource("src", {}, hardware=False)
    assert blk.error is None and fake_lib.opened == 0
    blk.close()


def test_bloc_rtlsdr_dongle_pris(monkeypatch):
    lib = FakeLib(open_error=rtl.ERR_BUSY)
    monkeypatch.setattr(RtlSdrSource, "open_dongle",
                        staticmethod(lambda i: rtl.Dongle(i, lib=lib)))
    blk = RtlSdrSource("src", {})
    assert "déjà pris" in blk.error


def test_bloc_rtlsdr_cadence_dans_le_trou(fake_lib):
    blk = RtlSdrSource("src", {"sample_rate": 500_000})
    assert "cadence impossible" in blk.error and fake_lib.opened == 0
