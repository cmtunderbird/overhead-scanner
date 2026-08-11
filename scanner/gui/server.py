"""
The operator interface.

A small FastAPI app that runs on the laptop, serves a single-page UI, and
talks to the camera nodes on your behalf.  It proxies rather than letting
the browser reach the nodes directly, so the GUI keeps working when only
the laptop is on the camera network, and so there is one place to put
timeouts and error text.

    python -m scanner gui --mock          # try it now, no hardware
    python -m scanner gui --node cam0=http://pi1:8000 --node cam1=http://pi2:8000

No build step, no npm: the UI is one HTML file.
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from ..geometry import FORMATS, A3, Lens, RigGeometry
from ..orchestrator.session import CaptureSession, NodeSpec

STATIC = Path(__file__).parent / "static"


@dataclass
class GuiState:
    nodes: list[NodeSpec] = field(default_factory=list)
    out_dir: str = "captures"
    profile: str = "standard"
    coverage: str = "stereo"
    overlap_mm: float = 20.0
    baseline_mm: float = 200.0
    document: str = "A3 spread"
    focal_mm: float = 30.0
    f_number: float = 8.0
    session: CaptureSession | None = None
    #: Peak focus score seen per camera+region, so the operator can tell
    #: when the ring has gone past the maximum.
    focus_peaks: dict = field(default_factory=dict)

    def geometry(self) -> RigGeometry:
        doc = FORMATS.get(self.document, A3)
        n = max(1, len(self.nodes))
        if self.coverage == "stereo" and n >= 2:
            return RigGeometry(n_cameras=n, coverage="stereo",
                               stereo_baseline_mm=self.baseline_mm, document=doc,
                               lens=Lens(self.focal_mm, self.f_number))
        return RigGeometry(n_cameras=n, coverage="tile",
                           overlap_mm=self.overlap_mm, document=doc,
                           lens=Lens(self.focal_mm, self.f_number))

    def ensure_session(self) -> CaptureSession:
        if self.session is None or [n.camera_id for n in self.session.nodes] != [
                n.camera_id for n in self.nodes]:
            self.session = CaptureSession(self.nodes, self.out_dir,
                                          profile=self.profile)
        return self.session


class NodesRequest(BaseModel):
    nodes: list[str]


class ConfigRequest(BaseModel):
    iso: int | None = None
    shutter: str | None = None
    aperture: str | None = None


class RigRequest(BaseModel):
    coverage: str | None = None
    overlap_mm: float | None = None
    baseline_mm: float | None = None
    document: str | None = None
    focal_mm: float | None = None
    f_number: float | None = None
    profile: str | None = None
    out_dir: str | None = None


class CaptureRequest(BaseModel):
    count: int = 1
    profile: str | None = None


def parse_node(spec: str, index: int) -> NodeSpec:
    cid, _, url = spec.partition("=")
    if not url:
        cid, url = f"cam{index}", spec
    return NodeSpec(cid, url, index)


def create_gui(state: GuiState) -> FastAPI:
    app = FastAPI(title="Overhead scanner", version="1.1",
                  summary="Operator interface")
    app.state.gui = state

    # ---------------------------------------------------------------- page

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    # --------------------------------------------------------------- state

    @app.get("/api/state")
    def get_state():
        g = state.geometry()
        nodes = []
        for n in state.nodes:
            entry = {"camera_id": n.camera_id, "url": n.base,
                     "tile_index": n.tile_index}
            try:
                r = requests.get(f"{n.base}/status", timeout=3.0)
                entry.update(r.json())
                entry["reachable"] = True
            except Exception as e:  # noqa: BLE001
                entry.update({"reachable": False, "connected": False,
                              "error": str(e)[:160]})
            nodes.append(entry)
        return {
            "nodes": nodes,
            "rig": {
                "coverage": state.coverage, "overlap_mm": state.overlap_mm,
                "baseline_mm": state.baseline_mm, "document": state.document,
                "focal_mm": state.focal_mm, "f_number": state.f_number,
                "profile": state.profile, "out_dir": state.out_dir,
                "formats": sorted(FORMATS),
            },
            "geometry": g.summary(),
            "throughput": state.session.throughput() if state.session else {},
            "captures": len(state.session.records) if state.session else 0,
        }

    @app.post("/api/nodes")
    def set_nodes(req: NodesRequest):
        state.nodes = [parse_node(s, i) for i, s in enumerate(req.nodes) if s.strip()]
        state.session = None
        return get_state()

    @app.post("/api/rig")
    def set_rig(req: RigRequest):
        for k, v in req.model_dump().items():
            if v is not None:
                setattr(state, k, v)
        if state.session is not None:
            state.session.profile = state.profile
        return get_state()

    @app.get("/api/geometry")
    def geometry(coverage: str | None = None, overlap_mm: float | None = None,
                 baseline_mm: float | None = None, document: str | None = None,
                 f_number: float | None = None):
        """Preview a configuration without committing to it."""
        saved = (state.coverage, state.overlap_mm, state.baseline_mm,
                 state.document, state.f_number)
        try:
            for name, val in (("coverage", coverage), ("overlap_mm", overlap_mm),
                              ("baseline_mm", baseline_mm), ("document", document),
                              ("f_number", f_number)):
                if val is not None:
                    setattr(state, name, val)
            try:
                return {"ok": True, "summary": state.geometry().summary(),
                        "report": state.geometry().report()}
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        finally:
            (state.coverage, state.overlap_mm, state.baseline_mm,
             state.document, state.f_number) = saved

    # ------------------------------------------------------------- cameras

    def _node(cam: str) -> NodeSpec:
        for n in state.nodes:
            if n.camera_id == cam:
                return n
        raise HTTPException(status_code=404, detail=f"no node called {cam!r}")

    @app.get("/api/preview/{cam}")
    def preview(cam: str):
        n = _node(cam)
        try:
            r = requests.get(f"{n.base}/preview", timeout=10.0)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(e)) from e
        return Response(content=r.content, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/stream/{cam}")
    def stream(cam: str, fps: float = 4.0):
        n = _node(cam)

        def relay():
            try:
                with requests.get(f"{n.base}/stream", params={"fps": fps},
                                  stream=True, timeout=30.0) as r:
                    for chunk in r.iter_content(chunk_size=16384):
                        if chunk:
                            yield chunk
            except Exception:  # noqa: BLE001
                return

        return StreamingResponse(
            relay(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/api/config")
    def config(req: ConfigRequest):
        payload = {k: v for k, v in req.model_dump().items() if v is not None}
        out = {}
        for n in state.nodes:
            try:
                r = requests.post(f"{n.base}/config", json=payload, timeout=10.0)
                out[n.camera_id] = {"ok": r.ok, "detail": r.json()}
            except Exception as e:  # noqa: BLE001
                out[n.camera_id] = {"ok": False, "detail": str(e)[:200]}
        return out

    @app.post("/api/connect/{cam}")
    def connect(cam: str):
        n = _node(cam)
        try:
            r = requests.post(f"{n.base}/connect", timeout=20.0)
            return {"ok": r.ok, "detail": r.json()}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(e)) from e

    # --------------------------------------------------------------- focus

    @app.get("/api/meter/{cam}")
    def meter(cam: str, iso: float | None = None, aperture: float | None = None):
        """
        Guided exposure: meter in A, shoot in M.

        The node does the arithmetic; this only carries it, so the operator
        never transcribes a number off the camera's screen and never has to
        remember that a meter renders white paper grey.
        """
        n = _node(cam)
        params = {}
        if iso is not None:
            params["iso"] = iso
        if aperture is not None:
            params["aperture"] = aperture
        try:
            r = requests.get(f"{n.base}/meter", params=params, timeout=90.0)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(e)) from e

    @app.get("/api/focus/{cam}")
    def focus(cam: str):
        """
        Capture a real frame and score sharpness in five regions.

        Peaks are held per region so the operator can see when the ring
        has gone past the maximum -- which is the only way to know, since
        the absolute number means nothing.
        """
        n = _node(cam)
        try:
            r = requests.get(f"{n.base}/focus", timeout=60.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(e)) from e

        peaks = state.focus_peaks.setdefault(cam, {})
        for reg in data.get("regions", []):
            best = max(peaks.get(reg["name"], 0.0), reg["score"])
            peaks[reg["name"]] = best
            reg["peak"] = round(best, 4)
            reg["relative"] = round(reg["score"] / best, 4) if best > 0 else 0.0
        return data

    @app.post("/api/focus/{cam}/reset")
    def focus_reset(cam: str):
        state.focus_peaks.pop(cam, None)
        return {"ok": True}

    # ------------------------------------------------------------- capture

    @app.post("/api/capture")
    def capture(req: CaptureRequest):
        if not state.nodes:
            raise HTTPException(status_code=400, detail="no nodes configured")
        session = state.ensure_session()
        out = []
        for _ in range(max(1, req.count)):
            rec = session.capture_spread(req.profile or state.profile)
            out.append(asdict(rec))
        return {"records": out, "throughput": session.throughput()}

    @app.post("/api/session/reset")
    def session_reset():
        state.session = None
        return {"ok": True}

    @app.get("/api/session/manifest")
    def manifest():
        if state.session is None:
            return {"captures": [], "throughput": {}}
        return {"captures": [asdict(r) for r in state.session.records],
                "throughput": state.session.throughput()}

    @app.get("/api/thumb")
    def thumb(path: str, width: int = 320):
        """Downscaled preview of a captured file, so the strip stays cheap."""
        import cv2

        p = Path(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"no such file {path}")
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(
                status_code=415,
                detail=f"cannot decode {p.name} -- RAW files need a decoder")
        h = max(1, int(width * img.shape[0] / img.shape[1]))
        ok, buf = cv2.imencode(".jpg", cv2.resize(img, (width, h)),
                               [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok:
            raise HTTPException(status_code=500, detail="encode failed")
        return Response(content=buf.tobytes(), media_type="image/jpeg")

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


# --------------------------------------------------------------------------
# mock rig, so the interface can be used with no hardware at all
# --------------------------------------------------------------------------


def start_mock_nodes(count: int = 2, scale: float = 0.12) -> list[str]:
    """Spin up simulated camera nodes in this process and return their URLs."""
    import socket

    import uvicorn

    from ..node.backends.mock import MockCamera
    from ..node.server import create_app

    urls = []
    for i in range(count):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        cam = MockCamera(f"cam{i}", i, scale=scale, page_px_per_mm=3.0,
                         transfer_mb_s=1e6)
        cam.connect()
        cfg = uvicorn.Config(create_app(cam), host="127.0.0.1", port=port,
                             log_level="error")
        server = uvicorn.Server(cfg)
        threading.Thread(target=server.run, daemon=True).start()
        urls.append(f"cam{i}=http://127.0.0.1:{port}")

    deadline = time.time() + 20
    for u in urls:
        base = u.split("=", 1)[1]
        while time.time() < deadline:
            try:
                if requests.get(f"{base}/healthz", timeout=0.5).ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
    return urls
