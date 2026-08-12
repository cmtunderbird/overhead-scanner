"""
The node API, exercised over real HTTP against the mock camera.

If the orchestrator is correct here it is correct against a Sony: the
mock drops its PTP session, takes realistic transfer time and returns a
different page each capture, which are the three behaviours that break
naive capture code.
"""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from scanner.geometry import RigGeometry, A3
from scanner.node import server
from scanner.node.backends.base import CameraDisconnected
from scanner.node.backends.mock import MockCamera

GEOM = RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)


@pytest.fixture
def client():
    cam = MockCamera("cam0", 0, GEOM, scale=0.06, page_px_per_mm=3.0,
                     transfer_mb_s=1e6)
    cam.connect()
    server.set_camera(cam)
    yield TestClient(server.app), cam
    server.set_camera(None)


def test_status_reports_the_camera(client):
    c, cam = client
    r = c.get("/status")
    assert r.status_code == 200
    d = r.json()
    assert d["camera_id"] == "cam0"
    assert d["backend"] == "mock"
    assert d["connected"] is True
    assert d["profiles"] == {"standard": 1, "clean": 3, "max": 9}


def test_config_round_trips(client):
    c, _ = client
    r = c.post("/config", json={"iso": 400, "aperture": "8.0"})
    assert r.status_code == 200
    s = r.json()["settings"]
    assert s["iso"] == 400 and s["aperture"] == "8.0"
    assert s["capture_target"] == "card"      # never left to the default


def test_capture_and_fetch(client):
    c, _ = client
    r = c.post("/capture", json={"seq": 7, "profile": "standard"})
    assert r.status_code == 200
    body = r.json()
    assert body["seq"] == 7 and len(body["files"]) == 1
    fid = body["files"][0]["file_id"]
    f = c.get(f"/files/{fid}")
    assert f.status_code == 200 and len(f.content) > 1000


@pytest.mark.parametrize("profile,frames", [("standard", 1), ("clean", 3)])
def test_profiles_return_the_right_frame_count(client, profile, frames):
    c, _ = client
    r = c.post("/capture", json={"seq": 1, "profile": profile})
    assert len(r.json()["files"]) == frames


def test_unknown_profile_is_rejected(client):
    c, _ = client
    r = c.post("/capture", json={"seq": 1, "profile": "ludicrous"})
    assert r.status_code == 400
    assert "ludicrous" in r.json()["detail"]


def test_missing_file_is_404(client):
    c, _ = client
    assert c.get("/files/nope").status_code == 404


def test_sequence_number_is_carried_through_not_invented(client):
    """Pairing depends on this: the node must echo the orchestrator's seq."""
    c, _ = client
    for seq in (0, 5, 99):
        r = c.post("/capture", json={"seq": seq})
        assert r.json()["seq"] == seq
        assert all(f["seq"] == seq for f in r.json()["files"])


def test_dropped_session_is_recovered_transparently():
    """The A6000 does this when idle.  It must not surface as an error."""
    cam = MockCamera("cam0", 0, GEOM, scale=0.06, page_px_per_mm=3.0,
                     transfer_mb_s=1e6, drop_every=2)
    cam.connect()
    server.set_camera(cam)
    c = TestClient(server.app)
    try:
        for seq in range(6):
            r = c.post("/capture", json={"seq": seq})
            assert r.status_code == 200, r.json()
        assert cam.connected
    finally:
        server.set_camera(None)


def test_capture_while_disconnected_reports_503():
    cam = MockCamera("cam0", 0, GEOM, scale=0.06, page_px_per_mm=3.0)
    server.set_camera(cam)          # never connected
    c = TestClient(server.app)
    try:
        cam.disconnect()
        cam.reconnect = lambda *a, **k: (_ for _ in ()).throw(
            CameraDisconnected("cable unplugged"))
        r = c.post("/capture", json={"seq": 0})
        assert r.status_code == 503
        assert "cable unplugged" in r.json()["detail"]
    finally:
        server.set_camera(None)


def test_preview_is_a_jpeg(client):
    c, _ = client
    r = c.get("/preview")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"      # SOI


def test_successive_captures_differ(client):
    """A mock that returns the same bytes forever hides real bugs."""
    c, _ = client
    a = c.get(f"/files/{c.post('/capture', json={'seq': 0}).json()['files'][0]['file_id']}").content
    b = c.get(f"/files/{c.post('/capture', json={'seq': 1}).json()['files'][0]['file_id']}").content
    assert a != b


# --------------------------------------------------------------------------
# GET /config: what the body says, against what the node believes
#
# Added after 12 Aug 2026, when the two disagreed on real hardware and
# nothing exposed it.  POST /config asked for f/5.6; the body applied it
# (EXIF confirmed F Number 5.6); the node compared '5.6' with the body's
# 'f/5.6' as strings, called it a rejection, and kept reporting f/8.0.
# --------------------------------------------------------------------------

class _ReadableMock(MockCamera):
    """A mock that can be asked what it is really set to."""
    live: dict[str, str] = {}

    def read_settings(self):
        return dict(self.live)


def _readable_client(live):
    cam = _ReadableMock("cam0", 0, GEOM, scale=0.06, page_px_per_mm=3.0,
                        transfer_mb_s=1e6)
    cam.live = live
    cam.connect()
    server.set_camera(cam)
    return TestClient(server.app), cam


def test_get_config_reports_agreement():
    c, cam = _readable_client({})
    try:
        # Body echoes exactly what the node believes.
        cam.live = {
            "iso": str(cam.settings.iso),
            "shutterspeed": cam.settings.shutter,
            "f-number": cam.settings.aperture,
        }
        d = c.get("/config").json()
        assert d["disagreements"] == []
        assert d["read_from_body"]["iso"] == str(cam.settings.iso)
    finally:
        server.set_camera(None)


def test_get_config_names_a_disagreement():
    """
    Silence here is the whole point: an empty list must mean agreement,
    not "did not look".
    """
    c, cam = _readable_client({})
    try:
        cam.live = {
            "iso": str(cam.settings.iso),
            "shutterspeed": cam.settings.shutter,
            "f-number": "5.6",           # node believes 8.0
        }
        d = c.get("/config").json()
        assert d["disagreements"] == ["f-number"]
        assert d["read_from_body"]["f-number"] == "5.6"
        assert d["node_believes"]["f-number"] == cam.settings.aperture
    finally:
        server.set_camera(None)


def test_get_config_on_a_backend_that_cannot_be_asked():
    """Unknown must never be rendered as agreement."""
    c, _ = _readable_client({})
    try:
        d = c.get("/config").json()
        assert d["read_from_body"] == {}
        assert d["disagreements"] == []      # nothing read, so nothing to compare
    finally:
        server.set_camera(None)


# ------------------------------------------------------- claiming frames ----
#
# The route that did not exist until 2026-08-12, and whose absence was the
# reason `release_cache()` sat in the backend with no caller: there was no
# way for the orchestrator to say "I have this frame, you may forget it".

def test_a_frame_can_be_claimed(client):
    c, _cam = client
    fid = c.post("/capture", json={"seq": 4}).json()["files"][0]["file_id"]
    assert c.get(f"/files/{fid}").status_code == 200

    r = c.delete(f"/files/{fid}")
    assert r.status_code == 200
    assert r.json()["existed"] is True
    assert r.json()["staged_frames"] == 0
    assert c.get(f"/files/{fid}").status_code == 404


def test_claiming_twice_is_not_an_error(client):
    """
    A release whose response was lost will be retried.  Answering 404 the
    second time invites the orchestrator to treat a frame it has already
    safely taken as one worth re-fetching.
    """
    c, _cam = client
    fid = c.post("/capture", json={"seq": 5}).json()["files"][0]["file_id"]
    assert c.delete(f"/files/{fid}").json()["existed"] is True
    again = c.delete(f"/files/{fid}")
    assert again.status_code == 200
    assert again.json()["existed"] is False


def test_reading_a_frame_does_not_claim_it(client):
    c, _cam = client
    fid = c.post("/capture", json={"seq": 6}).json()["files"][0]["file_id"]
    c.get(f"/files/{fid}")
    c.get(f"/files/{fid}")
    assert c.get("/staged").json()["count"] >= 1


def test_staged_lists_what_the_node_still_holds(client):
    c, _cam = client
    c.delete("/files")
    ids = [c.post("/capture", json={"seq": s}).json()["files"][0]["file_id"]
           for s in (7, 8)]
    body = c.get("/staged").json()
    assert body["count"] == 2
    assert set(ids) <= set(body["file_ids"])
    assert body["cap"] > 0


def test_release_all_empties_the_node(client):
    c, _cam = client
    c.post("/capture", json={"seq": 9})
    r = c.delete("/files")
    assert r.status_code == 200
    assert r.json()["staged_frames"] == 0
    assert c.get("/staged").json()["count"] == 0


def test_status_publishes_the_staged_count(client):
    c, _cam = client
    c.delete("/files")
    c.post("/capture", json={"seq": 10})
    st = c.get("/status").json()
    assert st["staged_frames"] == 1
    assert st["frames_evicted"] == 0
    assert "staging_volatile" in st


def test_the_calibrated_focal_length_can_be_recorded(client):
    """
    Set once, after the zoom is taped.  Zero means "not calibrated", which
    is the only honest default -- an alarm nobody has calibrated for gets
    ignored, including on the day it is right.
    """
    c, _cam = client
    assert c.get("/status").json()["expected_focal_length_mm"] == 0.0
    r = c.post("/optics", json={"expected_focal_length_mm": 30.0})
    assert r.status_code == 200
    assert r.json()["expected_focal_length_mm"] == 30.0
    assert c.get("/status").json()["expected_focal_length_mm"] == 30.0
