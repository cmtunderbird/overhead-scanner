"""
The orchestrator, driving two live HTTP nodes.

Real uvicorn servers on real sockets, so the code under test is the code
that will run tomorrow -- not a mocked-out transport.
"""
import socket
import threading
import time

import pytest
import uvicorn

from scanner.geometry import RigGeometry, A3
from scanner.node.server import create_app
from scanner.node.backends.mock import MockCamera
from scanner.orchestrator.session import CaptureSession, NodeSpec

GEOM = RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server(threading.Thread):
    def __init__(self, app, port):
        super().__init__(daemon=True)
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(cfg)

    def run(self):
        self.server.run()

    def stop(self):
        self.server.should_exit = True


@pytest.fixture
def rig_nodes():
    cams, servers, specs = [], [], []
    for i, cid in enumerate(["cam0", "cam1"]):
        cam = MockCamera(cid, i, GEOM, scale=0.05, page_px_per_mm=3.0,
                         transfer_mb_s=1e6, drop_every=5 if i == 0 else 0)
        cam.connect()
        port = _free_port()
        srv = _Server(create_app(cam), port)
        srv.start()
        cams.append(cam)
        servers.append(srv)
        specs.append(NodeSpec(cid, f"http://127.0.0.1:{port}", i))

    deadline = time.time() + 20
    import requests
    for sp in specs:
        while time.time() < deadline:
            try:
                if requests.get(f"{sp.base}/healthz", timeout=0.5).ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        else:
            pytest.fail(f"node {sp.camera_id} never came up")

    yield specs, cams
    for s in servers:
        s.stop()


def test_all_nodes_report_ready(rig_nodes, tmp_path):
    specs, _ = rig_nodes
    s = CaptureSession(specs, tmp_path)
    s.require_ready()
    st = s.check()
    assert set(st) == {"cam0", "cam1"}
    assert all(v["connected"] for v in st.values())


def test_settings_reach_every_node(rig_nodes, tmp_path):
    specs, cams = rig_nodes
    s = CaptureSession(specs, tmp_path)
    s.configure_all(iso=400, aperture="8.0", shutter="1/160")
    for c in cams:
        assert c.settings.iso == 400
        assert c.settings.aperture == "8.0"


def test_capture_pairs_frames_by_sequence_not_time(rig_nodes, tmp_path):
    specs, _ = rig_nodes
    s = CaptureSession(specs, tmp_path)
    recs = s.capture_many(3)
    assert [r.seq for r in recs] == [0, 1, 2]
    for r in recs:
        assert r.complete, r.errors
        assert set(r.files) == {"cam0", "cam1"}
        # both halves of a spread land in the same numbered directory
        dirs = {p.rsplit("/", 2)[-2] for paths in r.files.values() for p in paths}
        assert dirs == {f"{r.seq:05d}"}


def test_files_are_written_and_non_empty(rig_nodes, tmp_path):
    specs, _ = rig_nodes
    s = CaptureSession(specs, tmp_path)
    rec = s.capture_spread()
    from pathlib import Path
    for cid, paths in rec.files.items():
        assert paths
        for p in paths:
            assert Path(p).stat().st_size > 500


def test_a_dropped_ptp_session_does_not_lose_a_spread(rig_nodes, tmp_path):
    """cam0 drops its session every 5 frames; the run must not notice."""
    specs, cams = rig_nodes
    s = CaptureSession(specs, tmp_path)
    recs = s.capture_many(8)
    assert all(r.complete for r in recs), [r.errors for r in recs if r.errors]
    assert cams[0]._dropped_at, "the drop never fired; test proves nothing"


def test_burst_profile_pulls_every_frame(rig_nodes, tmp_path):
    specs, _ = rig_nodes
    s = CaptureSession(specs, tmp_path, profile="clean")
    rec = s.capture_spread()
    assert rec.complete
    assert all(len(p) == 3 for p in rec.files.values())


def test_one_unreachable_node_fails_that_spread_not_the_session(tmp_path):
    specs = [NodeSpec("cam0", f"http://127.0.0.1:{_free_port()}", 0)]
    s = CaptureSession(specs, tmp_path)
    rec = s.capture_spread()
    assert not rec.ok
    assert "cam0" in rec.errors
    # the session survives and keeps counting
    assert s.seq == 1
    with pytest.raises(RuntimeError, match="not ready"):
        s.require_ready()


def test_throughput_and_manifest(rig_nodes, tmp_path):
    import json
    specs, _ = rig_nodes
    s = CaptureSession(specs, tmp_path)
    s.capture_many(4)
    t = s.throughput()
    assert t["spreads"] == 4 and t["complete"] == 4 and t["failed"] == 0
    assert t["mean_s"] > 0 and t["mb_per_s"] > 0
    doc = json.loads(s.write_manifest().read_text())
    assert len(doc["captures"]) == 4
    assert doc["throughput"]["spreads"] == 4
    assert [c["seq"] for c in doc["captures"]] == [0, 1, 2, 3]
