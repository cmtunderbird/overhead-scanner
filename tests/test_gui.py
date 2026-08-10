"""
The operator interface, exercised over real HTTP against mock nodes.

The GUI is a proxy, so testing it against a `TestClient` alone would
prove nothing: every interesting failure -- an unreachable node, a slow
focus capture, a relay that dies mid-stream -- lives in the hop between
the laptop and the node.  These tests therefore run real uvicorn nodes
in-process and let the GUI talk to them over the loopback interface.
"""
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from scanner.gui.server import GuiState, create_gui, parse_node, start_mock_nodes
from scanner.metrics.focus import REGIONS, exposure_stats, focus_map, focus_score


# --------------------------------------------------------------------------
# focus scoring -- a unit, no server involved
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def textured():
    rng = np.random.default_rng(11)
    img = np.full((400, 600, 3), 235, np.uint8)
    for _ in range(300):
        x, y = rng.integers(10, 560), rng.integers(10, 380)
        w, h = rng.integers(6, 40), rng.integers(2, 5)
        img[y:y + h, x:x + w] = rng.integers(10, 70)
    return img


def test_focus_score_separates_sharp_from_blurred(textured):
    sharp = focus_score(textured)
    blurred = focus_score(cv2.GaussianBlur(textured, (0, 0), 2.0))
    assert sharp > 3 * blurred, (sharp, blurred)


def test_focus_score_is_monotonic_in_blur(textured):
    scores = [focus_score(textured if s == 0 else
                          cv2.GaussianBlur(textured, (0, 0), s))
              for s in (0, 0.8, 1.6, 3.2)]
    assert scores == sorted(scores, reverse=True), scores


def test_focus_map_covers_the_named_regions(textured):
    regions = focus_map(textured)
    assert [r.name for r in regions] == list(REGIONS)
    assert all(r.score > 0 for r in regions)


def test_focus_map_finds_the_corner_that_is_soft(textured):
    """A lens soft in one corner must show up as *that* corner."""
    img = textured.copy()
    h, w = img.shape[:2]
    y0, x0 = int(0.70 * h), int(0.70 * w)
    img[y0:, x0:] = cv2.GaussianBlur(img[y0:, x0:], (0, 0), 3.0)
    scores = {r.name: r.score for r in focus_map(img)}
    assert scores["bottom right"] == min(scores.values())
    assert scores["bottom right"] < 0.5 * scores["centre"]


def test_exposure_stats_sees_clipping():
    img = np.full((200, 200, 3), 128, np.uint8)
    img[:40] = 255
    img[40:60] = 0
    d = exposure_stats(img)
    assert d["clipped_high_pct"] == pytest.approx(20.0, abs=0.5)
    assert d["clipped_low_pct"] == pytest.approx(10.0, abs=0.5)
    assert len(d["histogram"]) == 64


# --------------------------------------------------------------------------
# the GUI against live nodes
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rig():
    specs = start_mock_nodes(2, scale=0.05)
    return [parse_node(s, i) for i, s in enumerate(specs)]


@pytest.fixture
def gui(rig, tmp_path):
    state = GuiState(nodes=list(rig), out_dir=str(tmp_path / "captures"))
    return TestClient(create_gui(state)), state


def test_it_serves_the_page(gui):
    c, _ = gui
    r = c.get("/")
    assert r.status_code == 200
    assert "Overhead Scanner" in r.text
    # no build step, no CDN: the whole interface must be self-contained
    assert "<script src=" not in r.text


def test_state_reports_every_node_and_the_geometry(gui):
    c, _ = gui
    d = c.get("/api/state").json()
    assert [n["camera_id"] for n in d["nodes"]] == ["cam0", "cam1"]
    assert all(n["reachable"] and n["connected"] for n in d["nodes"])
    assert d["geometry"]["coverage"] == "stereo"
    assert d["geometry"]["stereo_coverage_pct"] == 100.0
    assert "A3 spread" in d["rig"]["formats"]


def test_an_unreachable_node_is_reported_not_raised(rig, tmp_path):
    """One dead node must not take the interface down with it."""
    from scanner.orchestrator.session import NodeSpec

    dead = NodeSpec("cam9", "http://127.0.0.1:1", 9)
    state = GuiState(nodes=[rig[0], dead], out_dir=str(tmp_path))
    c = TestClient(create_gui(state))
    d = c.get("/api/state").json()
    assert d["nodes"][0]["reachable"] is True
    assert d["nodes"][1]["reachable"] is False
    assert d["nodes"][1]["error"]


def test_geometry_preview_does_not_commit(gui):
    c, state = gui
    before = (state.coverage, state.baseline_mm)
    d = c.get("/api/geometry", params={"coverage": "tile", "baseline_mm": 300}).json()
    assert d["ok"] and d["summary"]["coverage"] == "tile"
    assert (state.coverage, state.baseline_mm) == before


def test_geometry_preview_reports_an_impossible_rig(gui):
    c, _ = gui
    d = c.get("/api/geometry", params={"baseline_mm": 0.0}).json()
    assert d["ok"] is False and "baseline" in d["error"].lower()


def test_tile_and_stereo_disagree_the_way_the_optics_say(gui):
    """The interface must not paper over the mode trade-off."""
    c, _ = gui
    tile = c.get("/api/geometry", params={"coverage": "tile"}).json()["summary"]
    stereo = c.get("/api/geometry", params={"coverage": "stereo"}).json()["summary"]
    assert tile["dpi"] > stereo["dpi"]                     # tiling resolves more
    assert stereo["working_distance_mm"] > tile["working_distance_mm"]
    assert stereo["stereo_coverage_pct"] == 100.0          # ... but sees it all
    assert tile["stereo_coverage_pct"] < 10.0


def test_rig_changes_are_applied_to_the_session(gui):
    c, state = gui
    c.post("/api/capture", json={"count": 1})
    c.post("/api/rig", json={"profile": "clean", "baseline_mm": 260.0})
    assert state.profile == "clean" and state.session.profile == "clean"
    assert c.get("/api/state").json()["geometry"]["baseline_mm"] == 260.0


def test_setting_nodes_starts_a_new_session(gui, rig):
    c, state = gui
    c.post("/api/capture", json={"count": 1})
    assert state.session is not None
    c.post("/api/nodes", json={"nodes": [f"{n.camera_id}={n.base}" for n in rig]})
    assert state.session is None


def test_preview_returns_an_image(gui):
    c, _ = gui
    r = c.get("/api/preview/cam0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.size > 0


def test_an_unknown_camera_is_a_404(gui):
    c, _ = gui
    assert c.get("/api/preview/nope").status_code == 404
    assert c.get("/api/focus/nope").status_code == 404


def test_focus_scores_a_real_frame_not_the_preview(gui):
    """
    The live view is a fraction of the sensor's pixels and cannot show
    focus.  /focus must therefore go through a full capture.
    """
    c, _ = gui
    before = c.get("/api/state").json()["nodes"][0]["frames_captured"]
    d = c.get("/api/focus/cam0").json()
    after = c.get("/api/state").json()["nodes"][0]["frames_captured"]
    assert after > before
    assert [r["name"] for r in d["regions"]] == list(REGIONS)
    assert d["jpeg_b64"]
    prev = c.get("/api/preview/cam0").content
    prev_px = cv2.imdecode(np.frombuffer(prev, np.uint8), cv2.IMREAD_COLOR).shape
    assert d["size"][0] * d["size"][1] > prev_px[0] * prev_px[1]


def test_focus_holds_the_peak_so_you_can_tell_you_went_past_it(gui):
    c, state = gui
    d = c.get("/api/focus/cam0").json()
    assert all(r["relative"] == pytest.approx(1.0) for r in d["regions"])

    # pretend the ring has been turned too far
    for name in state.focus_peaks["cam0"]:
        state.focus_peaks["cam0"][name] *= 4.0
    d = c.get("/api/focus/cam0").json()
    assert all(r["relative"] < 0.4 for r in d["regions"])

    c.post("/api/focus/cam0/reset")
    d = c.get("/api/focus/cam0").json()
    assert all(r["relative"] == pytest.approx(1.0) for r in d["regions"])


def test_capture_writes_files_and_the_manifest_finds_them(gui):
    c, state = gui
    d = c.post("/api/capture", json={"count": 2}).json()
    assert len(d["records"]) == 2
    assert all(r["ok"] for r in d["records"])
    assert d["throughput"]["complete"] == 2

    m = c.get("/api/session/manifest").json()
    assert [r["seq"] for r in m["captures"]] == [0, 1]
    for rec in m["captures"]:
        assert set(rec["files"]) == {"cam0", "cam1"}
        for paths in rec["files"].values():
            assert Path(paths[0]).exists()


def test_thumbnails_come_back_downscaled(gui):
    c, _ = gui
    c.post("/api/capture", json={"count": 1})
    path = c.get("/api/session/manifest").json()["captures"][0]["files"]["cam0"][0]
    r = c.get("/api/thumb", params={"path": path, "width": 160})
    assert r.status_code == 200
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == 160


def test_a_missing_thumbnail_is_a_404_not_a_crash(gui):
    c, _ = gui
    assert c.get("/api/thumb", params={"path": "nope.arw"}).status_code == 404


def test_capture_without_nodes_is_refused(tmp_path):
    c = TestClient(create_gui(GuiState(nodes=[], out_dir=str(tmp_path))))
    assert c.post("/api/capture", json={"count": 1}).status_code == 400


def test_session_reset_clears_the_history(gui):
    c, _ = gui
    c.post("/api/capture", json={"count": 1})
    c.post("/api/session/reset")
    assert c.get("/api/session/manifest").json()["captures"] == []


def test_config_reaches_every_camera(gui):
    c, _ = gui
    d = c.post("/api/config", json={"iso": 400, "aperture": "8.0"}).json()
    assert set(d) == {"cam0", "cam1"}
    assert all(v["ok"] for v in d.values())
    st = c.get("/api/state").json()
    assert all(n["settings"]["iso"] == 400 for n in st["nodes"])


def test_node_specs_parse_both_forms():
    assert parse_node("cam1=http://pi:8000", 0).camera_id == "cam1"
    n = parse_node("http://pi:8000", 3)
    assert (n.camera_id, n.base, n.tile_index) == ("cam3", "http://pi:8000", 3)
