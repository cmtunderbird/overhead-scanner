"""
Staging headroom: refuse before the shutter, and never pretend a frame
that was not written.

Reproduced on `scanner-node-0` (Pi 5, ILCE-6000) on 2026-08-12 by filling
the node's staging tmpfs and asking for one frame:

    dd if=/dev/zero of=<staging>/fill bs=1M      # to ENOSPC
    curl -X POST .../capture -d '{"seq":201,"profile":"standard"}'
    -> Internal Server Error        (bare text, no cause given)
    -> journal: OSError: [Errno 28] No space left on device
                at gphoto.py _shoot -> dest.write_bytes(data)

The shutter had fired and the full 24.5 MB had already been transferred
off the body before the write failed.  Both were spent for nothing, and
the operator was told only "Internal Server Error" -- which sends them to
the camera, the one place the fault was not.
"""
import pytest
from fastapi.testclient import TestClient

from scanner.geometry import RigGeometry, A3
from scanner.node import server
from scanner.node.backends.base import CameraBackend, StagingFull
from scanner.node.backends.mock import MockCamera

GEOM = RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)

MB = 1024 * 1024


class StagedMock(MockCamera):
    """A mock with a staging area whose free space the test dictates."""

    def __init__(self, *a, free_bytes: int = -1, **kw):
        super().__init__(*a, **kw)
        self._free = free_bytes

    def staging_free_bytes(self) -> int:
        return self._free

    def capture(self, seq: int, frames: int = 1):
        self.require_staging_space(frames)
        return super().capture(seq, frames)


def _cam(free_bytes=-1):
    c = StagedMock("cam0", 0, GEOM, scale=0.06, page_px_per_mm=3.0,
                   transfer_mb_s=1e6, free_bytes=free_bytes)
    c.connect()
    return c


# -- the preflight itself ----------------------------------------------------

def test_no_staging_area_means_no_preflight():
    """A backend that stages nothing must not be second-guessed."""
    cam = _cam(free_bytes=-1)
    cam.require_staging_space(9)          # must not raise
    assert cam.status().staging_free_mb == -1


def test_ample_space_passes():
    cam = _cam(free_bytes=2000 * MB)
    cam.require_staging_space(9)


def test_refuses_when_a_single_frame_will_not_fit():
    cam = _cam(free_bytes=10 * MB)
    with pytest.raises(StagingFull) as e:
        cam.require_staging_space(1)
    assert "10 MB free" in str(e.value)
    # The message must say the shutter was not used, because the whole
    # point of preflighting is that it was not.
    assert "No shutter actuation" in str(e.value)


def test_margin_is_demanded_on_top_of_the_frames():
    """
    Room for exactly the frames is not enough.

    Refusing at the precise moment the filesystem stops accepting writes
    leaves nothing for a log line or a diagnostic, so the node keeps a
    margin in hand and declines earlier.
    """
    cam = _cam(free_bytes=CameraBackend.NOMINAL_FRAME_BYTES)   # exactly one frame
    with pytest.raises(StagingFull):
        cam.require_staging_space(1)


def test_a_burst_needs_room_for_every_frame():
    """One frame's headroom must not authorise a nine-frame profile."""
    free = CameraBackend.NOMINAL_FRAME_BYTES + CameraBackend.STAGING_MARGIN_BYTES + MB
    cam = _cam(free_bytes=free)
    cam.require_staging_space(1)                 # fine
    with pytest.raises(StagingFull):
        cam.require_staging_space(9)             # nine of them are not


def test_estimate_follows_the_largest_frame_actually_seen():
    """
    The preflight sizes itself from measurement, not from a constant.

    A body that turns out to produce larger frames than NOMINAL must not
    keep being waved through on the old figure.
    """
    big = 90 * MB
    free = 3 * CameraBackend.NOMINAL_FRAME_BYTES + CameraBackend.STAGING_MARGIN_BYTES
    cam = _cam(free_bytes=free)
    cam.require_staging_space(3)                 # passes on the nominal estimate
    cam._peak_frame_bytes = big                  # now we know better
    with pytest.raises(StagingFull):
        cam.require_staging_space(3)


# -- how it surfaces over HTTP ----------------------------------------------

def test_status_publishes_the_headroom():
    """
    The operator must be able to see this coming.

    On a node the staging area is a tmpfs inside PrivateTmp, so `df` on the
    host reports a filesystem with gigabytes free while staging is full.
    /status is the only place the real number is visible.
    """
    cam = _cam(free_bytes=1536 * MB)
    server.set_camera(cam)
    try:
        d = TestClient(server.app).get("/status").json()
        assert d["staging_free_mb"] == 1536
    finally:
        server.set_camera(None)


def test_full_staging_is_507_not_503():
    """
    503 says "try again"; retrying a full disk fires the shutter again.

    507 Insufficient Storage says what is actually wrong, and the detail
    names the staging area so nobody goes back to the camera looking for a
    fault that is not there.
    """
    cam = _cam(free_bytes=1 * MB)
    server.set_camera(cam)
    try:
        r = TestClient(server.app).post(
            "/capture", json={"seq": 1, "profile": "standard"})
        assert r.status_code == 507
        detail = r.json()["detail"]
        assert "staging" in detail.lower()
        assert "1 MB free" in detail
    finally:
        server.set_camera(None)


def test_a_healthy_node_still_captures():
    """The preflight must not become a new way for captures to fail."""
    cam = _cam(free_bytes=4000 * MB)
    server.set_camera(cam)
    try:
        r = TestClient(server.app).post(
            "/capture", json={"seq": 1, "profile": "clean"})
        assert r.status_code == 200
        assert len(r.json()["files"]) == 3
    finally:
        server.set_camera(None)
