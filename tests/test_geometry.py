"""The optical model must reproduce the blueprint, exactly."""
import math
import pytest
from scanner.geometry import (RigGeometry, BLUEPRINT_V1, SINGLE_CAMERA_A3,
                              SINGLE_CAMERA_A4, A3, A4, SONY_A6000, Lens)


def test_blueprint_v1_reproduces_the_decision_of_record():
    g = BLUEPRINT_V1
    assert g.tile_width_mm == pytest.approx(220.0)
    assert g.dpi == pytest.approx(461.8, abs=0.1)
    assert g.spread_megapixels == pytest.approx(41.2, abs=0.1)
    assert g.working_distance_mm == pytest.approx(453.1, abs=0.2)
    assert g.depth_of_field_mm == pytest.approx(22.8, abs=0.2)
    assert g.baseline_mm == pytest.approx(200.0)
    assert g.stereo_dz_um == pytest.approx(26.7, abs=0.3)
    assert g.focal_px == pytest.approx(7692, abs=2)


def test_it_beats_the_czur():
    """The entire justification for the build."""
    g = BLUEPRINT_V1
    assert g.dpi > 441.0
    assert g.spread_megapixels > 39.8


def test_tiles_tile_the_document():
    for n in (1, 2, 3):
        for ov in (0.0, 10.0, 20.0, 40.0):
            g = RigGeometry(n_cameras=n, overlap_mm=ov)
            tiles = g.tiles
            assert len(tiles) == n
            assert tiles[0].x0_mm == pytest.approx(0.0)
            assert tiles[-1].x1_mm == pytest.approx(g.document.long_mm)
            for a, b in zip(tiles, tiles[1:]):
                assert a.x1_mm - b.x0_mm == pytest.approx(ov)


def test_overlap_increases_working_distance_and_costs_dpi():
    lo = RigGeometry(overlap_mm=10.0)
    hi = RigGeometry(overlap_mm=40.0)
    assert hi.tile_width_mm > lo.tile_width_mm
    assert hi.dpi < lo.dpi
    assert hi.working_distance_mm > lo.working_distance_mm
    assert hi.depth_of_field_mm > lo.depth_of_field_mm


def test_orientation_is_chosen_not_assumed():
    """
    Splitting the long axis wants portrait; a single camera on A3 wants
    landscape.  Getting this wrong is the difference between beating the
    CZUR and losing to it.
    """
    assert BLUEPRINT_V1.resolved_orientation == "portrait"
    assert SINGLE_CAMERA_A3.resolved_orientation == "landscape"
    forced = RigGeometry(n_cameras=1, document=A3, orientation="portrait")
    assert forced.dpi < SINGLE_CAMERA_A3.dpi


def test_single_camera_on_a4_already_beats_the_czur():
    """What one body can do the day it arrives."""
    assert SINGLE_CAMERA_A4.dpi > 441.0


def test_auto_orientation_never_loses_to_a_forced_one():
    for n in (1, 2):
        for doc in (A3, A4):
            auto = RigGeometry(n_cameras=n, document=doc)
            for o in ("portrait", "landscape"):
                assert auto.dpi >= RigGeometry(n_cameras=n, document=doc,
                                               orientation=o).dpi - 1e-9


def test_field_covers_the_document():
    g = BLUEPRINT_V1
    assert g.field_along_mm >= g.document.short_mm
    assert g.spare_field_mm == pytest.approx(g.field_along_mm - g.document.short_mm)


def test_stopping_down_costs_resolution():
    """f/8 is already diffraction limited; the model must say so."""
    assert BLUEPRINT_V1.diffraction_limited
    wide = RigGeometry(lens=Lens(30.0, 4.0))
    assert not wide.diffraction_limited
    assert wide.depth_of_field_mm < BLUEPRINT_V1.depth_of_field_mm


def test_single_camera_has_no_stereo():
    assert math.isinf(SINGLE_CAMERA_A3.stereo_dz_um)
    assert SINGLE_CAMERA_A3.baseline_mm == 0.0


def test_rejects_nonsense():
    with pytest.raises(ValueError):
        RigGeometry(n_cameras=0)
    with pytest.raises(ValueError):
        RigGeometry(n_cameras=2, overlap_mm=500.0)


def test_summary_is_serialisable():
    import json
    json.dumps(BLUEPRINT_V1.summary())
