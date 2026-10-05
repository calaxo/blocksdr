"""Le traitement du signal critique : continuité d'un bloc à l'autre, et justesse."""

from __future__ import annotations

import numpy as np

from app.dsp import (Decimator, Fir, FmDiscriminator, Nco, lowpass_taps,
                     spectrum_db)

SR = 240_000.0


def tone(f, n, sr=SR, phase0=0):
    t = (phase0 + np.arange(n)) / sr
    return np.exp(2j * np.pi * f * t).astype(np.complex64)


def in_chunks(proc, x, sizes):
    out, i = [], 0
    for n in sizes:
        out.append(proc(x[i:i + n]))
        i += n
    return np.concatenate(out)


def test_filtre_continu_entre_blocs():
    """Filtrer par morceaux = filtrer d'un coup (sinon : un clic par bloc)."""
    x = (np.random.default_rng(1).normal(size=5000)
         + 1j * np.random.default_rng(2).normal(size=5000)).astype(np.complex64)
    taps = lowpass_taps(20_000, SR, 63)
    whole = Fir(taps)(x)
    parts = in_chunks(Fir(taps), x, [1000, 1, 2345, 1654])
    np.testing.assert_allclose(parts, whole, atol=1e-5)


def test_decimation_garde_la_phase():
    """Le piège du `y[::d]` qui repart de 0 à chaque bloc (ronflement à 36 Hz)."""
    x = tone(3_000, 9_999)
    whole = Decimator(20_000, SR, 10)(x)
    parts = in_chunks(Decimator(20_000, SR, 10), x, [1234, 4321, 4444])
    np.testing.assert_allclose(parts, whole, atol=1e-5)


def test_decimation_coupe_hors_bande():
    """Une porteuse au-delà de la nouvelle bande ne doit PAS se replier."""
    dec = Decimator(0.45 * SR / 8, SR, 8, 127)
    y = dec(tone(100_000, 64_000))[200:]            # 100 kHz >> 15 kHz de bande
    assert 20 * np.log10(np.sqrt(np.mean(np.abs(y) ** 2))) < -40


def test_nco_deplace_et_reste_continu():
    x = tone(50_000, 8_192)
    y = Nco()(x, 50_000, SR)                          # ramène la porteuse au centre
    assert np.allclose(y, y[0], atol=1e-3)
    nco = Nco()
    parts = in_chunks(lambda b: nco(b, 50_000, SR), x, [1000, 3000, 4192])
    np.testing.assert_allclose(parts, y, atol=1e-3)


def test_spectre_pic_au_bon_endroit():
    n = 2048
    s = spectrum_db(tone(SR / 8, n * 4), n, "hann")
    assert len(s) == n and s.dtype == np.float32
    assert int(np.argmax(s)) == n // 2 + n // 8       # fftshift : 0 Hz au milieu


def test_spectre_bloc_trop_court_complete():
    assert len(spectrum_db(tone(1000, 100), 1024)) == 1024


def test_discriminateur_fm():
    """Fréquence constante f -> sortie constante f / (sr/2), sans clic au raccord."""
    x = tone(12_000, 4_000)
    disc = FmDiscriminator()
    y = in_chunks(disc, x, [1000, 3000])
    np.testing.assert_allclose(y[1:], 12_000 / (SR / 2), atol=1e-4)
