"""
The exposure calculator.

This is the one piece of the guided calibration that can be checked without
a camera in the room, so it is checked hard. Three things carry real risk of
being confidently wrong:

  * shutter parsing -- Sony reports 1/2 s as '5/10', and a parser that only
    understands '1/x' mis-reads the entire slow half of the range;
  * gamma -- levels come from a gamma-encoded JPEG, so stops computed on raw
    levels underestimate the correction by more than half;
  * snapping -- nearest-in-seconds picks a shutter up to half a stop out,
    because exposure error is logarithmic.

Each has a test that fails if the naive version is restored.
"""
from __future__ import annotations

import io
import math

import pytest
from PIL import Image

from scanner.calib.exposure import (
    ASSUMED_STOPS,
    ExposureReading,
    FrameStats,
    ev100,
    extract_preview,
    format_shutter,
    histogram_stats,
    parse_shutter,
    read_exif_exposure,
    read_frame,
    recommend,
    snap_to_choices,
    stops_to_target,
)

# The real list, as reported by the ILCE-6000 over PTP.
A6000_SHUTTERS = [
    "30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10", "25/10",
    "2", "16/10", "13/10", "1", "8/10", "6/10", "5/10", "4/10", "1/3", "1/4",
    "1/5", "1/6", "1/8", "1/10", "1/13", "1/15", "1/20", "1/25", "1/30",
    "1/40", "1/50", "1/60", "1/80", "1/100", "1/125", "1/160", "1/200",
    "1/250", "1/320", "1/400", "1/500", "1/640", "1/800", "1/1000", "Bulb",
]


# ------------------------------------------------------------- parsing ----

@pytest.mark.parametrize("text,expected", [
    ("1/250", 0.004),
    ("1/60", 1 / 60),
    ("2", 2.0),
    ("30", 30.0),
    ("1", 1.0),
    ("5/10", 0.5),      # Sony's unreduced form for 1/2 s
    ("32/10", 3.2),     # ...and for 3.2 s
    ("8/10", 0.8),
    ("0.5", 0.5),
    (" 1/125 ", 1 / 125),
])
def test_parse_shutter_handles_every_form_the_body_uses(text, expected):
    assert parse_shutter(text) == pytest.approx(expected)


def test_unreduced_fractions_are_not_read_as_fast_shutters():
    """
    '5/10' is half a second. Read carelessly it looks like 1/10 -- a five
    times error, and in the dangerous direction (too little exposure).
    """
    assert parse_shutter("5/10") == pytest.approx(0.5)
    assert parse_shutter("5/10") != pytest.approx(0.1)


@pytest.mark.parametrize("bad", ["Bulb", "", "auto", "1/0", "abc"])
def test_unusable_values_raise(bad):
    with pytest.raises(ValueError):
        parse_shutter(bad)


def test_bulb_is_dropped_not_treated_as_a_speed():
    labels = [lbl for _, lbl in
              [(s, l) for s, l in __import__(
                  "scanner.calib.exposure", fromlist=["usable_choices"]
              ).usable_choices(A6000_SHUTTERS)]]
    assert "Bulb" not in labels
    assert len(labels) == len(A6000_SHUTTERS) - 1


def test_format_shutter_round_trips():
    for s in ("1/250", "1/60", "2", "30"):
        assert parse_shutter(format_shutter(parse_shutter(s))) == \
               pytest.approx(parse_shutter(s))


# ------------------------------------------------------------ snapping ----

def test_snap_picks_nearest_in_stops_not_seconds():
    """
    Wanting 1/90 s: 1/60 and 1/125 straddle it. In seconds 1/60 (0.0167) is
    3.5 ms away and 1/125 (0.0080) is 3.1 ms away, so nearest-in-seconds
    picks 1/125. In stops they are 0.58 and 0.47 away -- 1/125 again, but
    now for the right reason. Use a case where the two disagree:
    """
    # 0.9 s: neighbours are 8/10 (0.8) and 1 (1.0).
    # In seconds: |0.9-0.8| = 0.1, |1.0-0.9| = 0.1 -- a tie.
    # In stops:   0.170 vs 0.152 -- 1 s wins, unambiguously.
    secs, label = snap_to_choices(0.9, A6000_SHUTTERS)
    assert label == "1"
    assert secs == pytest.approx(1.0)


def test_snap_returns_a_real_choice():
    for target in (0.003, 0.02, 1.7, 25.0):
        secs, label = snap_to_choices(target, A6000_SHUTTERS)
        assert label in A6000_SHUTTERS
        assert secs == pytest.approx(parse_shutter(label))


def test_snap_handles_an_empty_or_useless_list():
    assert snap_to_choices(0.01, []) is None
    assert snap_to_choices(0.01, ["Bulb"]) is None


# ----------------------------------------------------------------- EV ----

def test_ev100_matches_known_values():
    # EV 15 is f/16 at 1/125, ISO 100 -- log2(256 * 125) = 14.97.
    # (The "sunny 16" rule of 1/ISO gives 1/100 here, which is EV 14.64:
    # the rule is a shooting shortcut, not the definition of EV 15.)
    assert ev100(16, 1 / 125, 100) == pytest.approx(15.0, abs=0.05)
    assert ev100(16, 1 / 100, 100) == pytest.approx(14.64, abs=0.05)
    # f/8, 1/60, ISO 100
    assert ev100(8, 1 / 60, 100) == pytest.approx(11.9, abs=0.05)


def test_iso_shifts_ev_by_the_right_number_of_stops():
    base = ev100(8, 1 / 60, 100)
    assert ev100(8, 1 / 60, 400) == pytest.approx(base - 2.0, abs=1e-6)


# --------------------------------------------------------------- gamma ----

def test_stops_are_computed_in_linear_light():
    """
    Paper at 118 needs to reach 230. Naively that is log2(230/118) = 0.96
    stops. In linear light it is 2.2x that -- about 2.1 stops. Getting this
    wrong leaves every scan a stop short.
    """
    naive = math.log2(230 / 118)
    actual = stops_to_target(118.0, 230.0)
    assert actual == pytest.approx(2.2 * naive, rel=1e-6)
    assert actual > naive * 2


def test_already_correct_needs_no_change():
    assert stops_to_target(230.0, 230.0) == pytest.approx(0.0)


def test_overexposed_asks_for_less():
    assert stops_to_target(250.0, 230.0) < 0


# --------------------------------------------------- the recommendation ----

def _reading(paper=118.0, clipped=0.0, **kw):
    stats = FrameStats(paper_level=paper, mean_level=paper * 0.8,
                       clipped_high_pct=clipped, clipped_low_pct=0.0)
    return ExposureReading(iso=kw.get("iso", 100.0),
                           aperture=kw.get("aperture", 8.0),
                           shutter_s=kw.get("shutter_s", 1 / 250),
                           stats=stats)


def test_measured_basis_is_used_when_a_histogram_exists():
    rec = recommend(_reading(), A6000_SHUTTERS)
    assert rec.basis == "measured"
    assert rec.stops_applied == pytest.approx(stops_to_target(118.0))


def test_assumed_basis_says_so_when_nothing_was_measured():
    r = ExposureReading(iso=100, aperture=8.0, shutter_s=1 / 250, stats=None)
    rec = recommend(r, A6000_SHUTTERS)
    assert rec.basis == "assumed"
    assert rec.stops_applied == ASSUMED_STOPS
    assert any("assumed" in w for w in rec.warnings)


def test_grey_metered_paper_is_corrected_upwards():
    """The core case: meter renders paper grey, we push it back to white."""
    rec = recommend(_reading(paper=118.0, shutter_s=1 / 250), A6000_SHUTTERS)
    assert rec.stops_applied > 1.5
    assert rec.shutter_s > 1 / 250      # longer exposure
    assert rec.shutter_label == "1/60"


def test_a_clipping_metering_frame_is_flagged():
    rec = recommend(_reading(paper=252.0, clipped=6.0), A6000_SHUTTERS)
    assert any("clip" in w for w in rec.warnings)
    assert rec.stops_applied < 0        # come down


def test_exposure_is_held_constant_when_iso_is_overridden():
    """
    Metered at ISO 800; we intend to shoot ISO 100. That is 3 stops less
    sensitivity, so the shutter must lengthen by 3 stops or the change
    silently darkens every scan.
    """
    base = recommend(_reading(iso=800, shutter_s=1 / 500), A6000_SHUTTERS)
    swapped = recommend(_reading(iso=800, shutter_s=1 / 500),
                        A6000_SHUTTERS, iso=100)
    ratio = swapped.requested_shutter_s / base.requested_shutter_s
    assert math.log2(ratio) == pytest.approx(3.0, abs=0.01)
    assert swapped.iso == 100


def test_exposure_is_held_constant_when_aperture_is_overridden():
    """Metered wide open at f/4, shooting at f/8: two stops to make up."""
    base = recommend(_reading(aperture=4.0), A6000_SHUTTERS)
    swapped = recommend(_reading(aperture=4.0), A6000_SHUTTERS, aperture=8.0)
    ratio = swapped.requested_shutter_s / base.requested_shutter_s
    assert math.log2(ratio) == pytest.approx(2.0, abs=0.01)


def test_a_long_shutter_is_called_out_as_a_lighting_problem():
    rec = recommend(_reading(paper=40.0, shutter_s=1.0), A6000_SHUTTERS)
    assert any("vibration" in w for w in rec.warnings)


def test_snap_error_is_reported():
    rec = recommend(_reading(), A6000_SHUTTERS)
    assert abs(rec.snap_error_stops) < 0.5
    assert rec.shutter_label in A6000_SHUTTERS


def test_recommendation_survives_a_body_with_no_shutter_list():
    rec = recommend(_reading(), [])
    assert any("no usable shutter" in w for w in rec.warnings)


# ------------------------------------------------------- frame analysis ----

def _jpeg(level: int, size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("L", size, level).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_histogram_stats_on_a_flat_patch():
    st = histogram_stats(_jpeg(200))
    assert st.paper_level == pytest.approx(200, abs=3)
    assert st.mean_level == pytest.approx(200, abs=3)
    assert st.clipped_high_pct == 0.0


def test_paper_level_ignores_the_text():
    """
    A page is mostly paper with dark text on it. The mean is dragged down by
    the ink; the 90th percentile is not. Exposing for the mean would blow the
    paper out.
    """
    im = Image.new("L", (100, 100), 235)
    for y in range(0, 100, 4):          # ~25% of the frame is ink
        for x in range(100):
            im.putpixel((x, y), 20)
    buf = io.BytesIO(); im.save(buf, format="JPEG", quality=95)
    st = histogram_stats(buf.getvalue())
    assert st.paper_level == pytest.approx(235, abs=6)
    assert st.mean_level < 200          # mean is misleading here


def test_clipping_is_detected():
    st = histogram_stats(_jpeg(255))
    assert st.clipped_high_pct > 90


def test_extract_preview_takes_the_largest_embedded_jpeg():
    small, big = _jpeg(100, (16, 16)), _jpeg(180, (200, 200))
    blob = b"II*\x00" + b"\x00" * 40 + small + b"\x00" * 20 + big + b"\x00" * 10
    got = extract_preview(blob)
    assert got == big
    assert histogram_stats(got).paper_level == pytest.approx(180, abs=4)


def test_extract_preview_on_a_file_with_none():
    assert extract_preview(b"II*\x00" + b"\x00" * 200) is None


# ---------------------------------------------------------------- EXIF ----

def _tiff_with_exif(iso=100, fnum=(8, 1), etime=(1, 250)) -> bytes:
    """A minimal little-endian TIFF carrying an EXIF IFD with three tags."""
    import struct
    header = b"II" + struct.pack("<HI", 42, 8)

    exif_entries = [
        (0x829A, 5, 1, None, etime),    # ExposureTime, rational
        (0x829D, 5, 1, None, fnum),     # FNumber, rational
        (0x8827, 3, 1, iso, None),      # ISO, short
    ]
    ifd0_off = 8
    ifd0 = struct.pack("<H", 1)
    exif_ifd_off = ifd0_off + 2 + 12 + 4
    ifd0 += struct.pack("<HHII", 0x8769, 4, 1, exif_ifd_off)
    ifd0 += struct.pack("<I", 0)

    n = len(exif_entries)
    data_off = exif_ifd_off + 2 + n * 12 + 4
    body, blob = struct.pack("<H", n), b""
    for tag, typ, cnt, short_val, rat in exif_entries:
        if rat is not None:
            body += struct.pack("<HHII", tag, typ, cnt, data_off + len(blob))
            blob += struct.pack("<II", *rat)
        else:
            body += struct.pack("<HHI", tag, typ, cnt) + \
                    struct.pack("<HH", short_val, 0)
    body += struct.pack("<I", 0)
    return header + ifd0 + body + blob


def test_read_exif_exposure():
    got = read_exif_exposure(_tiff_with_exif(iso=400, fnum=(56, 10),
                                             etime=(1, 125)))
    assert got["iso"] == 400
    assert got["aperture"] == pytest.approx(5.6)
    assert got["shutter_s"] == pytest.approx(1 / 125)


def test_read_exif_on_whole_seconds():
    got = read_exif_exposure(_tiff_with_exif(etime=(2, 1)))
    assert got["shutter_s"] == pytest.approx(2.0)


def test_read_exif_on_rubbish_returns_empty_not_garbage():
    assert read_exif_exposure(b"not a tiff at all") == {}
    assert read_exif_exposure(b"") == {}
    assert read_exif_exposure(b"II*\x00\xff\xff\xff\xff") == {}


def test_read_frame_combines_exif_and_histogram():
    raw = _tiff_with_exif(iso=100, fnum=(8, 1), etime=(1, 250)) + _jpeg(120)
    r = read_frame(raw)
    assert r is not None
    assert r.iso == 100 and r.aperture == pytest.approx(8.0)
    assert r.shutter_s == pytest.approx(1 / 250)
    assert r.stats is not None
    assert r.stats.paper_level == pytest.approx(120, abs=4)


def test_read_frame_without_exif_is_none_not_a_guess():
    assert read_frame(_jpeg(120)) is None


def test_end_to_end_a_real_looking_metering_frame():
    """
    The whole path: a frame metered at f/8 1/250 ISO 100 whose paper landed
    at middle grey should come back as roughly 1/60 -- two stops slower.
    """
    raw = _tiff_with_exif(iso=100, fnum=(8, 1), etime=(1, 250)) + _jpeg(118)
    rec = recommend(read_frame(raw), A6000_SHUTTERS)
    assert rec.basis == "measured"
    assert rec.shutter_label == "1/60"
    assert rec.iso == 100 and rec.aperture == pytest.approx(8.0)
