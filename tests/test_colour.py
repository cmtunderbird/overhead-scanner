"""CIEDE2000 against the Sharma et al. reference pairs."""
import numpy as np
import pytest

from scanner.metrics.colour import (delta_e_2000, srgb_to_lab, srgb_to_linear,
                                    linear_to_srgb, sample_patch)
from scanner.calib.field import fit_colour_matrix, apply_colour_matrix

SHARMA = [
    ((50, 2.6772, -79.7751), (50, 0, -82.7485), 2.0425),
    ((50, 3.1571, -77.2803), (50, 0, -82.7485), 2.8615),
    ((50, 2.8361, -74.0200), (50, 0, -82.7485), 3.4412),
    ((50, -1.3802, -84.2814), (50, 0, -82.7485), 1.0000),
    ((50, 2.4900, -0.0010), (50, -2.4900, 0.0009), 7.1792),
    ((50, 2.5000, 0.0000), (73, 25.0000, -18.0000), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


@pytest.mark.parametrize("lab1,lab2,expected", SHARMA)
def test_ciede2000_reference_pairs(lab1, lab2, expected):
    got = float(delta_e_2000(np.array(lab1), np.array(lab2)))
    assert got == pytest.approx(expected, abs=1e-3)


def test_delta_e_of_a_colour_with_itself_is_zero():
    lab = srgb_to_lab(np.array([[123, 45, 200], [10, 200, 30]]))
    assert np.allclose(delta_e_2000(lab, lab), 0.0, atol=1e-9)


def test_srgb_transfer_roundtrips():
    x = np.linspace(0, 1, 257)
    assert np.allclose(linear_to_srgb(srgb_to_linear(x)), x, atol=1e-9)


def test_known_lab_landmarks():
    white = srgb_to_lab(np.array([255.0, 255.0, 255.0]))
    assert white[0] == pytest.approx(100.0, abs=0.05)
    assert abs(white[1]) < 0.02 and abs(white[2]) < 0.02
    black = srgb_to_lab(np.array([0.0, 0.0, 0.0]))
    assert black[0] == pytest.approx(0.0, abs=1e-6)


def test_colour_matrix_undoes_a_channel_cast():
    """The twin-camera seam, in miniature."""
    rng = np.random.default_rng(0)
    ref = rng.integers(20, 240, size=(24, 3)).astype(np.float64)
    gains = np.array([1.14, 0.99, 0.86])
    measured = linear_to_srgb(np.clip(srgb_to_linear(ref / 255) * gains, 0, 1)) * 255

    before = float(np.mean(delta_e_2000(srgb_to_lab(measured), srgb_to_lab(ref))))
    M = fit_colour_matrix(measured, ref)
    corrected = linear_to_srgb(np.clip(srgb_to_linear(measured / 255) @ M.T, 0, 1)) * 255
    after = float(np.mean(delta_e_2000(srgb_to_lab(corrected), srgb_to_lab(ref))))

    assert before > 2.0
    assert after < 0.3
    assert after < before / 10


def test_a_few_percent_of_channel_mismatch_is_already_near_the_limit():
    """
    Why the blueprint's dE < 2 between bodies is a real constraint: a 3-4 %
    per-channel difference -- entirely normal between two copies of the
    same camera -- lands around dE 1, which is visible as a band on a
    stitched spread precisely because it is a straight edge down the
    middle rather than a gradual shift.
    """
    rng = np.random.default_rng(4)
    ref = rng.integers(20, 240, size=(24, 3)).astype(np.float64)
    gains = np.array([1.035, 0.995, 0.962])       # BODY_B in the simulator
    measured = linear_to_srgb(np.clip(srgb_to_linear(ref / 255) * gains, 0, 1)) * 255
    de = float(np.mean(delta_e_2000(srgb_to_lab(measured), srgb_to_lab(ref))))
    assert 0.4 < de < 2.0

    M = fit_colour_matrix(measured, ref)
    corrected = linear_to_srgb(np.clip(srgb_to_linear(measured / 255) @ M.T, 0, 1)) * 255
    assert float(np.mean(delta_e_2000(srgb_to_lab(corrected), srgb_to_lab(ref)))) < 0.1


def test_identity_matrix_is_a_near_noop_on_an_image():
    img = (np.random.default_rng(1).integers(0, 256, (32, 32, 3))).astype(np.uint8)
    out = apply_colour_matrix(img, np.eye(3))
    assert np.abs(out.astype(int) - img.astype(int)).max() <= 1


def test_sample_patch_ignores_a_speck():
    patch = np.full((40, 40, 3), 180, np.uint8)
    patch[18:22, 18:22] = 0          # dust
    got = sample_patch(patch, 0, 0, 40, 40)
    assert np.allclose(got, 180, atol=1)
