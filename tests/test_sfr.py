"""
The SFR implementation is validated against an analytic oracle.

Two traps caught during development, both worth stating:
  * the oracle must include the 1-pixel sensor aperture (sinc), not just
    the optical blur -- a slanted-edge measurement sees the whole system;
  * cv2.GaussianBlur at small sigma is NOT a continuous Gaussian, so the
    oracle is built from the discrete kernel OpenCV actually applies.
Skip either and the code looks 40 % wrong when it is 1 % right.
"""
import cv2
import numpy as np
import pytest
from scipy.optimize import brentq

from scanner.metrics.sfr import measure_sfr, gaussian_mtf50


def synth_edge(size=160, angle=5.0, sigma=0.0, ss=8, gamma=2.2, noise=0.0, seed=0):
    th = np.radians(angle)
    c, s = np.cos(th), np.sin(th)
    ys, xs = np.mgrid[0:size * ss, 0:size * ss]
    cx = cy = size * ss / 2
    d = (xs + 0.5 - cx) * c - (ys + 0.5 - cy) * s
    f = np.where(d < 0, 0.02, 0.95).astype(np.float64)
    f = f.reshape(size, ss, size, ss).mean(axis=(1, 3))
    if sigma > 0:
        f = cv2.GaussianBlur(f, (0, 0), sigma, borderType=cv2.BORDER_REPLICATE)
    enc = np.power(np.clip(f, 1e-6, 1), 1 / gamma) * 255
    if noise:
        enc = enc + np.random.default_rng(seed).normal(0, noise, enc.shape)
    return np.clip(enc, 0, 255).astype(np.uint8)


def oracle_mtf50(sigma):
    """MTF50 of the discrete kernel cv2 applies, times the pixel aperture."""
    k = cv2.getGaussianKernel(int(2 * round(3 * sigma) + 1), sigma).ravel()
    n = np.arange(len(k)) - len(k) // 2
    def mtf(f):
        return abs((k * np.cos(2 * np.pi * f * n)).sum()) * abs(np.sinc(f))
    return brentq(lambda f: mtf(f) - 0.5, 1e-6, 0.9999)


@pytest.mark.parametrize("sigma", [0.4, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0])
def test_mtf50_matches_theory(sigma):
    got = measure_sfr(synth_edge(sigma=sigma)).mtf50
    assert got == pytest.approx(oracle_mtf50(sigma), rel=0.05)


@pytest.mark.parametrize("angle", [2.0, 5.0, 8.0, -5.0, 12.0])
def test_edge_angle_is_recovered(angle):
    r = measure_sfr(synth_edge(angle=angle, sigma=1.0))
    assert r.angle_deg == pytest.approx(angle, abs=0.1)


@pytest.mark.parametrize("angle", [2.0, 5.0, -5.0, 10.0])
def test_mtf50_is_independent_of_edge_angle(angle):
    """A measurement that depends on how you laid the target down is not one."""
    r = measure_sfr(synth_edge(angle=angle, sigma=1.0))
    assert r.mtf50 == pytest.approx(oracle_mtf50(1.0), rel=0.07)


def test_sharper_edges_read_sharper():
    vals = [measure_sfr(synth_edge(sigma=s)).mtf50 for s in (0.6, 1.0, 1.6, 2.4)]
    assert all(a > b for a, b in zip(vals, vals[1:]))


@pytest.mark.parametrize("noise", [0.0, 1.0, 3.0])
def test_tolerates_sensor_noise(noise):
    got = measure_sfr(synth_edge(sigma=1.0, noise=noise)).mtf50
    assert got == pytest.approx(oracle_mtf50(1.0), rel=0.12)


def test_gaussian_oracle_helper():
    assert gaussian_mtf50(1.0) == pytest.approx(0.1874, abs=1e-4)


def test_refuses_an_edge_it_cannot_measure():
    with pytest.raises(ValueError):
        measure_sfr(synth_edge(angle=0.0, sigma=1.0))     # no phase coverage
    with pytest.raises(ValueError):
        measure_sfr(np.full((10, 10, 3), 128, np.uint8))  # too small


def test_blueprint_acceptance_threshold_is_measurable():
    """
    The acceptance criterion is MTF50 >= 0.30 cy/px.  Confirm the harness
    resolves that region accurately, since it sits near the sharp end.
    """
    sigma = 0.55
    r = measure_sfr(synth_edge(sigma=sigma))
    assert 0.25 < r.mtf50 < 0.40
    assert r.mtf50 == pytest.approx(oracle_mtf50(sigma), rel=0.06)
