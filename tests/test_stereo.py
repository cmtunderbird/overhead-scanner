"""
Stereoscopic surface recovery and page flattening.

The claim under test is the one the whole rig is for: two cameras that
both see the whole spread can measure the page's 3-D shape and flatten
it, and doing so removes a distortion that a homography structurally
cannot.
"""
import numpy as np
import pytest

from scanner.geometry import A3, RigGeometry, STEREO_A3
from scanner.stereo import (arc_length_error, cameras_from_capture,
                            dewarp_stereo, dewarp_view, sweep_surface)
from scanner.synth.camera import BODY_A, BODY_B
from scanner.synth.page import render_page
from scanner.synth.render3d import (camera_matrix, render_surface,
                                    stereo_pair_poses)
from scanner.synth.surface import BOOKS, BookSurface, fit_developable

#: Render scale for the fixtures.  Surface recovery is resolution
#: dependent -- fewer pixels means weaker correlation and a coarser
#: surface -- so the thresholds below are stated for THIS scale.  The
#: real rig runs at 1.0 and does better; dropping to 0.20 roughly
#: quadruples the paper-coordinate error.
SCALE = 0.25
PAGE_PPM = 5.0


# ---------------------------------------------------------------- surface ---

def test_flat_surface_degenerates_to_a_plane():
    f = BOOKS["flat"]
    x = np.linspace(0, 420, 21)
    assert np.allclose(f.height(x), 0.0)
    # arc length becomes plain distance from the spine
    assert np.allclose(f.arc_length(x), x - f.spine_x_mm, atol=1e-9)


def test_arc_length_round_trips():
    b = BOOKS["hardback"]
    s = np.linspace(-200, 200, 41)
    x, y, z = b.to_platen(s, np.zeros_like(s))
    s2, _ = b.to_page(x, y)
    assert np.abs(s2 - s).max() < 1e-6


def test_curvature_hides_paper():
    """A curved page is longer than its shadow -- that is foreshortening."""
    for name in ("paperback", "hardback", "tome"):
        b = BOOKS[name]
        assert b.page_span_mm(0, 420) > 420.0
    assert BOOKS["tome"].page_span_mm(0, 420) > BOOKS["paperback"].page_span_mm(0, 420)


def test_developable_fit_beats_its_own_input_noise():
    """
    The physics is the point: every sample at the same x measures the
    same height, so thousands of noisy points collapse to a far better
    surface than any one of them.
    """
    b = BOOKS["hardback"]
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 420, 150_000)
    z = b.height(x) + rng.normal(0, 0.050, x.size)      # 50 um per point
    fit = fit_developable(x, z, x_min=0, x_max=420)
    xt = np.linspace(20, 400, 401)
    rms = float(np.sqrt(np.mean((fit.height(xt) - b.height(xt)) ** 2)))
    assert rms < 0.030                                   # better than 30 um
    assert abs(fit.spine_x_mm - b.spine_x_mm) < 5.0


# --------------------------------------------------------------- geometry ---

def test_stereo_mode_covers_the_whole_document():
    g = STEREO_A3
    assert g.is_stereo
    assert g.stereo_coverage_fraction == 1.0
    for t in g.tiles:
        assert t.x0_mm == 0.0 and t.x1_mm == A3.long_mm


def test_tiling_forces_the_baseline_but_stereo_frees_it():
    """
    The structural result: widening the overlap to gain stereo coverage
    drags the cameras together, so depth precision gets *worse* as
    coverage improves.  Only full overlap breaks the coupling.
    """
    narrow = RigGeometry(overlap_mm=20.0)
    wide = RigGeometry(overlap_mm=300.0)
    assert wide.stereo_coverage_fraction > narrow.stereo_coverage_fraction
    assert wide.baseline_mm < narrow.baseline_mm          # cameras converge
    assert wide.stereo_dz_um > narrow.stereo_dz_um        # precision collapses

    free = RigGeometry(coverage="stereo", stereo_baseline_mm=300.0)
    assert free.stereo_coverage_fraction == 1.0
    assert free.stereo_dz_um < wide.stereo_dz_um


def test_stereo_meets_the_dewarping_budget_at_a_sane_baseline():
    """~50 um is what resampling text at 462 DPI demands."""
    assert RigGeometry(coverage="stereo", stereo_baseline_mm=200.0).stereo_dz_um < 50.0
    assert RigGeometry(coverage="stereo", stereo_baseline_mm=150.0).stereo_dz_um > 50.0


def test_stereo_costs_resolution_and_buys_depth_of_field():
    tile = RigGeometry(overlap_mm=20.0)
    st = STEREO_A3
    assert st.dpi < tile.dpi                              # the price
    assert st.depth_of_field_mm > tile.depth_of_field_mm  # longer standoff
    assert st.convergence_deg > 10.0


def test_stereo_config_is_validated():
    with pytest.raises(ValueError):
        RigGeometry(n_cameras=1, coverage="stereo")
    with pytest.raises(ValueError):
        RigGeometry(coverage="stereo", stereo_baseline_mm=0.0)


# ------------------------------------------------------------------ render ---

@pytest.fixture(scope="module")
def stereo_capture():
    surf = BOOKS["hardback"]
    paper = surf.page_span_mm(0, 420)
    page, _ = render_page(paper, 297.0, px_per_mm=PAGE_PPM, spread=True, seed=3)
    pitch = 0.0039 / SCALE
    size = (int(6000 * SCALE), int(4000 * SCALE))
    K = camera_matrix(30.0, pitch, size)
    dist = np.array([BODY_A.k1, BODY_A.k2, BODY_A.p1, BODY_A.p2, 0.0])
    pa, pb = stereo_pair_poses(200.0, 600.0)
    caps = [
        render_surface(page, PAGE_PPM, surf, K, dist, p, size, b,
                       focus_height_mm=surf.rise_mm / 2, pixel_pitch_mm=pitch, seed=i)
        for i, (p, b) in enumerate([(pa, BODY_A), (pb, BODY_B)])
    ]
    return surf, caps


def test_renderer_height_map_matches_the_surface(stereo_capture):
    surf, caps = stereo_capture
    c = caps[0]
    err = np.abs(c.height_mm - surf.height(c.platen_x_mm))
    assert np.nanmax(err) < 1e-3


def test_both_cameras_see_most_of_the_page(stereo_capture):
    _, caps = stereo_capture
    for c in caps:
        assert c.valid.mean() > 0.6


def test_curvature_actually_shows_up_in_the_images(stereo_capture):
    """A flat renderer would put the same paper at the same pixel."""
    surf, caps = stereo_capture
    a, b = caps
    ok = a.valid & b.valid
    assert ok.sum() > 1000
    # the two views disagree about where a given pixel's paper is
    assert np.nanmax(np.abs(a.page_s_mm[ok] - b.page_s_mm[ok])) > 5.0


# ------------------------------------------------------------------- sweep ---

@pytest.fixture(scope="module")
def swept(stereo_capture):
    surf, caps = stereo_capture
    ca, cb = (cameras_from_capture(c) for c in caps)
    sw = sweep_surface(caps[0].image, caps[1].image, ca, cb,
                       x_range_mm=(5, 415), height_range_mm=(-5, 45),
                       x_step_mm=1.5, height_step_mm=0.25, n_y_samples=320)
    return surf, caps, sw


def test_sweep_recovers_the_surface(swept):
    surf, _, sw = swept
    assert abs(np.nanmax(sw.height_mm) - surf.rise_mm) < 2.0
    assert abs(sw.spine_x_mm - surf.spine_x_mm) < 6.0
    xt = np.linspace(20, 400, 381)
    rms = float(np.sqrt(np.mean((sw.surface.height(xt) - surf.height(xt)) ** 2)))
    assert rms < 0.30


def test_sweep_rejects_texture_free_columns(swept):
    """
    Peak *height* is not confidence: two smooth gradients correlate at
    0.95 whatever depth you assume.  Peak *width* is what separates a
    page of text from a blank margin, and the blank gutter must be
    rejected rather than believed.
    """
    _, _, sw = swept
    assert sw.unreliable.any(), "nothing rejected -- the test proves nothing"
    assert (~sw.unreliable).mean() > 0.4
    good = sw.peak_width_mm[~sw.unreliable]
    bad = sw.peak_width_mm[sw.unreliable]
    assert np.nanmedian(good) < np.nanmedian(bad)


def test_recovered_paper_coordinate_is_good_enough_to_resample_text(swept):
    surf, _, sw = swept
    xt = np.linspace(20, 400, 381)
    err = arc_length_error(surf, sw.surface, xt)
    # Height error matters only through arc length, and arc length
    # integrates the *slope* -- so a wrong height at the textureless
    # gutter apex costs far less than it looks like it should.
    assert err["rms_mm"] < 0.15          # < 3 px at 462 DPI
    assert err["max_mm"] < 0.30


# ------------------------------------------------------------------ dewarp ---

def _block_shifts(img, ref, n=9):
    import cv2
    h, w = ref.shape[:2]
    gh, gw = h // n, w // n
    v = []
    for r in range(1, n - 1):
        for c in range(1, n - 1):
            A = cv2.cvtColor(ref[r*gh:(r+1)*gh, c*gw:(c+1)*gw], cv2.COLOR_BGR2GRAY).astype(np.float32)
            B = cv2.cvtColor(img[r*gh:(r+1)*gh, c*gw:(c+1)*gw], cv2.COLOR_BGR2GRAY).astype(np.float32)
            if A.std() < 4 or B.std() < 4:
                continue
            win = cv2.createHanningWindow((A.shape[1], A.shape[0]), cv2.CV_32F)
            (dx, dy), resp = cv2.phaseCorrelate(A * win, B * win)
            if resp > 0.05:
                v.append((dx, dy))
    return np.array(v)


def test_stereo_dewarp_beats_assuming_the_page_is_flat(swept):
    """
    The headline. A homography cannot correct height-induced parallax --
    it is a plane-to-plane map and the page is not a plane.  Measuring
    the surface and resampling against it removes the distortion a
    homography leaves behind.
    """
    surf, caps, sw = swept
    ca, cb = (cameras_from_capture(c) for c in caps)
    kw = dict(s_range_mm=(-180, 180), y_range_mm=(10, 287), page_px_per_mm=4.0)

    ref = dewarp_stereo(caps[0].image, caps[1].image, ca, cb, surf, **kw).image
    rec = dewarp_stereo(caps[0].image, caps[1].image, ca, cb, sw.surface, **kw).image
    flat = dewarp_view(caps[0].image, BookSurface(profile="flat", rise_mm=0.0),
                       ca["K"], ca["dist"], ca["R"], ca["C"], **kw)[0]

    def residual(img):
        v = _block_shifts(img, ref)
        assert len(v) > 8
        return np.hypot(*(v - np.median(v, axis=0)).T)   # a rigid shift is a crop

    r_stereo = np.median(residual(rec))
    r_flat = np.median(residual(flat))
    assert r_stereo < 1.0
    assert r_flat > 3.0
    assert r_stereo < r_flat / 5.0


def test_fusion_prefers_the_near_camera_at_the_gutter(swept):
    """
    Each page half is imaged most squarely by the camera on its own side
    -- 21 deg off-normal versus 41 deg at 10 mm from the spine.  The
    weights must reflect that rather than averaging blindly.
    """
    surf, caps, sw = swept
    ca, cb = (cameras_from_capture(c) for c in caps)
    res = dewarp_stereo(caps[0].image, caps[1].image, ca, cb, surf,
                        s_range_mm=(-180, 180), y_range_mm=(10, 287),
                        page_px_per_mm=3.0)
    wa, wb = res.weights
    n = wa.shape[1]
    left = slice(int(n * 0.30), int(n * 0.45))    # left page, near the spine
    right = slice(int(n * 0.55), int(n * 0.70))   # right page, near the spine
    assert np.nanmean(wa[:, left]) > np.nanmean(wb[:, left])
    assert np.nanmean(wb[:, right]) > np.nanmean(wa[:, right])
