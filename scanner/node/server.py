"""
Node HTTP API.

One of these runs on each Raspberry Pi, next to one camera.  It is
deliberately thin: it holds no session state beyond the camera itself,
because the orchestrator on the laptop owns the sequence counter and the
pairing.  If a node reboots mid-book, you lose one spread, not the run.

    uvicorn scanner.node.server:app --host 0.0.0.0 --port 8000

Environment:
    SCANNER_CAMERA_ID   cam0 | cam1        (default: from hostname)
    SCANNER_TILE        0 | 1              (default: 0)
    SCANNER_BACKEND     mock | gphoto2     (default: auto)
    SCANNER_MOCK_SCALE  render scale for the mock backend
"""

from __future__ import annotations

import os
import socket
from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from .backends.base import CameraBackend, CameraError, CameraSettings

def _default_camera_id() -> str:
    """Role from hostname, so both Pis can run the identical SD image."""
    host = socket.gethostname().lower()
    if "1" in host.split("-")[-1]:
        return "cam1"
    return "cam0"


def build_camera() -> CameraBackend:
    camera_id = os.environ.get("SCANNER_CAMERA_ID") or _default_camera_id()
    tile = int(os.environ.get("SCANNER_TILE", "1" if camera_id.endswith("1") else "0"))
    backend = os.environ.get("SCANNER_BACKEND", "auto").lower()

    if backend == "auto":
        from .backends.gphoto import gphoto2_available

        backend = "gphoto2" if gphoto2_available() else "mock"

    if backend == "gphoto2":
        from .backends.gphoto import GPhotoCamera

        return GPhotoCamera(camera_id, tile)

    from .backends.mock import MockCamera

    return MockCamera(
        camera_id, tile, scale=float(os.environ.get("SCANNER_MOCK_SCALE", "0.25"))
    )


class ConfigRequest(BaseModel):
    iso: int | None = None
    shutter: str | None = None
    aperture: str | None = None
    capture_target: str | None = None
    image_format: str | None = None


class CaptureRequest(BaseModel):
    #: Issued by the orchestrator.  Pairing is by this, never by timestamp.
    seq: int
    profile: str = "standard"


PROFILE_FRAMES = {"standard": 1, "clean": 3, "max": 9}


def create_app(camera: CameraBackend | None = None) -> FastAPI:
    """
    Build a node app.

    The camera lives on `app.state`, not in a module global, so several
    nodes can run inside one process -- which is exactly what the
    orchestrator tests need, and what lets you rehearse the whole
    two-camera rig on a laptop with no cameras at all.
    """
    api = FastAPI(
        title="Overhead scanner node",
        version="1.0",
        summary="One camera, one Pi, one HTTP surface.",
    )
    api.state.camera = camera
    api.state.autobuild = camera is None

    def cam_of(request: Request) -> CameraBackend:
        st = request.app.state
        if st.camera is None:
            if not st.autobuild:
                raise HTTPException(status_code=503, detail="no camera configured")
            st.camera = build_camera()
            try:
                st.camera.connect()
            except CameraError:
                pass  # /status will report it; do not refuse to start
        return st.camera

    @api.get("/status")
    def status(request: Request):
        d = asdict(cam_of(request).status())
        d["profiles"] = PROFILE_FRAMES
        return d

    @api.post("/connect")
    def connect(request: Request):
        cam = cam_of(request)
        try:
            cam.reconnect()
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return asdict(cam.status())

    @api.post("/config")
    def configure(request: Request, req: ConfigRequest):
        cam = cam_of(request)
        try:
            cam.apply_settings(cam.settings.merged(**req.model_dump()))
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return asdict(cam.status())

    @api.get("/preview")
    def preview(request: Request):
        try:
            return Response(content=cam_of(request).preview(), media_type="image/jpeg")
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @api.post("/capture")
    def capture(request: Request, req: CaptureRequest):
        cam = cam_of(request)
        frames = PROFILE_FRAMES.get(req.profile)
        if frames is None:
            raise HTTPException(
                status_code=400,
                detail=f"unknown profile {req.profile!r}; expected one of "
                f"{sorted(PROFILE_FRAMES)}",
            )
        try:
            files = cam.capture_with_retry(req.seq, frames)
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return {
            "seq": req.seq,
            "camera_id": cam.camera_id,
            "profile": req.profile,
            "files": [asdict(f) for f in files],
        }

    @api.get("/files/{file_id:path}")
    def get_file(request: Request, file_id: str):
        cam = cam_of(request)
        try:
            data = cam.read_file(file_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"no such file {file_id}") from e
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        ctype = "image/png" if cam.status().backend == "mock" else "image/x-sony-arw"
        return Response(content=data, media_type=ctype)

    @api.get("/healthz")
    def healthz():
        return {"ok": True}

    return api


#: The app uvicorn serves on a node.  Builds its camera from the environment.
app = create_app()


def set_camera(cam: CameraBackend | None) -> None:
    """Injection point for tests against the default app."""
    app.state.camera = cam
    app.state.autobuild = cam is None


def get_camera() -> CameraBackend | None:
    return app.state.camera
