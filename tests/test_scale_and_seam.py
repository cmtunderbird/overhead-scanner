import cv2
import numpy as np
import pytest

from scanner.metrics.scale import measure_scale, measure_seam


def _marks(w=600, h=200, ax=60, bx=540, y=120, r=6):
    img = np.full((h, w, 3), 240, np.uint8)
    cv2.circle(img, (ax, y), r, (10, 10, 10), -1)
    cv2.circle(img, (bx, y), r, (10, 10, 10), -1)
    return img


def test_measures_px_per_mm():
    img = _marks()
    res = measure_scale(img, (55, 118), (546, 123), span_mm=100.0, search_px=25)
    assert res.px_per_mm == pytest.approx(4.8, rel=1e-3)      # 480 px / 100 mm
    assert res.dpi == pytest.approx(4.8 * 25.4, rel=1e-3)


def test_centroid_is_subpixel_and_robust_to_the_initial_guess():
    img = _marks()
    a = measure_scale(img, (60, 120), (540, 120), 100.0, search_px=25)
    b = measure_scale(img, (48, 131), (551, 109), 100.0, search_px=30)
    assert a.px_per_mm == pytest.approx(b.px_per_mm, rel=1e-6)


def test_noise_does_not_move_the_answer_much():
    rng = np.random.default_rng(0)
    img = _marks().astype(np.float32) + rng.normal(0, 4, (200, 600, 3))
    img = np.clip(img, 0, 255).astype(np.uint8)
    res = measure_scale(img, (60, 120), (540, 120), 100.0, search_px=25)
    assert res.px_per_mm == pytest.approx(4.8, rel=3e-3)


def test_reports_error_against_a_prediction():
    res = measure_scale(_marks(), (60, 120), (540, 120), 100.0, search_px=25)
    assert res.error_vs(4.8 * 25.4) == pytest.approx(0.0, abs=1e-3)


def test_raises_when_there_is_no_mark():
    img = np.full((100, 100, 3), 240, np.uint8)
    with pytest.raises(ValueError):
        measure_scale(img, (50, 50), (80, 50), 10.0, search_px=10)


def _texture(seed=0, shift=(0, 0)):
    rng = np.random.default_rng(seed)
    base = rng.integers(60, 200, (240, 240)).astype(np.uint8)
    base = cv2.GaussianBlur(base, (0, 0), 2.0)
    M = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
    a = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR)
    b = cv2.cvtColor(cv2.warpAffine(base, M, (240, 240),
                                    borderMode=cv2.BORDER_REFLECT), cv2.COLOR_GRAY2BGR)
    return a, b


def test_perfect_registration_reads_near_zero():
    a, b = _texture()
    res = measure_seam(a, b, 40, 200)
    assert res.shift_px < 0.15
    assert res.mean_abs_diff < 1.0


@pytest.mark.parametrize("dx", [1.0, 2.0, 4.0])
def test_recovers_a_known_misregistration(dx):
    a, b = _texture(shift=(dx, 0))
    res = measure_seam(a, b, 40, 200)
    assert abs(res.dx_px) == pytest.approx(dx, abs=0.25)
    assert res.shift_px == pytest.approx(dx, abs=0.25)


def test_rejects_an_overlap_that_is_too_narrow():
    a, b = _texture()
    with pytest.raises(ValueError):
        measure_seam(a, b, 100, 103)
