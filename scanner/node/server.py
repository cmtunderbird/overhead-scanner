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

import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .backends.base import CameraBackend, CameraError, CameraSettings, StagingFull

def _default_camera_id() -> str:
    """Role from hostname, so both Pis can run the identical SD image."""
    host = socket.gethostname().lower()
    if "1" in host.split("-")[-1]:
        return "cam1"
    return "cam0"



def _parse_shutter_safe(value) -> float:
    """Shutter string to seconds, without importing the calibration module
    at call sites that may not need it."""
    from ..calib.exposure import parse_shutter
    return parse_shutter(value)

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

    @api.get("/config")
    def read_config(request: Request):
        """
        What the body is set to, asked now — as against what we believe.

        `/status` reports the node's own record. This asks the camera. The
        two should agree, and on 2026-08-12 they did not: a read-back
        comparison failed on notation (`'5.6'` vs the body's `'f/5.6'`), so
        a correctly applied aperture was recorded as rejected and `/status`
        kept the previous value. Without this route there was no way to see
        that from outside the machine.
        """
        cam = cam_of(request)
        try:
            live = cam.read_settings()
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        believed = {
            "iso": str(cam.settings.iso),
            "shutterspeed": cam.settings.shutter,
            "f-number": cam.settings.aperture,
        }
        return {
            "camera_id": cam.camera_id,
            "read_from_body": live,
            "node_believes": believed,
            #: Empty is the healthy answer. Anything here means the node is
            #: reporting an exposure the camera does not have.
            "disagreements": sorted(
                k for k, v in live.items()
                if k in believed and str(believed[k]).strip() != v
            ),
        }

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

    @api.get("/stream")
    def stream(request: Request, fps: float = 4.0):
        """
        Live view as MJPEG.

        The A6000 gives roughly 1-3 fps over PTP at about 1024x680 -- a
        thirty-fifth of the sensor's pixels.  That is enough to frame a
        page and nowhere near enough to judge focus, which is why
        /focus exists separately.
        """
        cam = cam_of(request)
        interval = 1.0 / max(fps, 0.2)

        def frames():
            while True:
                t0 = time.perf_counter()
                try:
                    jpg = cam.preview()
                except CameraError:
                    break
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                       b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n"
                       + jpg + b"\r\n")
                dt = time.perf_counter() - t0
                if dt < interval:
                    time.sleep(interval - dt)

        return StreamingResponse(
            frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    @api.get("/meter")
    def meter(request: Request,
              iso: float | None = None,
              aperture: float | None = None):
        """
        One metering frame -> the settings to dial in M.

        `expprogram` is read-only over PTP, so the operator puts the body in
        A, and the camera solves for shutter. We read back what it chose,
        measure where the paper actually landed, and correct.

        The correction matters: a meter renders what it sees as middle grey,
        so aimed at white paper it underexposes by about two stops. Copying
        the metered value straight into M gives dark scans.
        """
        from ..calib.exposure import (
            ExposureReading, histogram_stats, read_frame, recommend,
        )

        cam = cam_of(request)
        try:
            files = cam.capture_with_retry(0, 1)
            data = cam.read_file(files[0].file_id)
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        reading = read_frame(data)
        source = "exif"
        if reading is None:
            # No EXIF (mock backend, or a body that writes JPEG-only).
            # Fall back to what the camera says it is set to, and measure
            # the frame we just took. Say which, so nobody mistakes a
            # fallback for a reading off the file.
            source = "settings"
            try:
                stats = histogram_stats(data)
            except Exception:  # noqa: BLE001
                stats = None
            try:
                shutter_s = _parse_shutter_safe(cam.settings.shutter)
                reading = ExposureReading(
                    iso=float(cam.settings.iso),
                    aperture=float(str(cam.settings.aperture).lstrip("f/")),
                    shutter_s=shutter_s,
                    stats=stats,
                )
            except (ValueError, TypeError) as e:
                raise HTTPException(
                    status_code=503,
                    detail=f"no EXIF in the frame and the camera's own settings "
                           f"could not be parsed either: {e}") from e

        choices = cam.config_choices("shutterspeed")
        rec = recommend(reading, choices, iso=iso, aperture=aperture)

        return {
            "source": source,
            "metered": {
                "iso": reading.iso,
                "aperture": reading.aperture,
                "shutter_s": reading.shutter_s,
                "ev100": round(reading.ev, 2),
                "paper_level": round(reading.stats.paper_level, 1) if reading.stats else None,
                "mean_level": round(reading.stats.mean_level, 1) if reading.stats else None,
                "clipped_high_pct": round(reading.stats.clipped_high_pct, 2) if reading.stats else None,
            },
            "recommend": {
                "iso": rec.iso,
                "aperture": rec.aperture,
                "shutter": rec.shutter_label,
                "shutter_s": rec.shutter_s,
                "stops_applied": round(rec.stops_applied, 2),
                "basis": rec.basis,
                "snap_error_stops": round(rec.snap_error_stops, 2),
            },
            "choices_known": bool(choices),
            "warnings": rec.warnings,
        }

    @api.get("/focus")
    def focus(request: Request):
        """
        Score sharpness on a real captured frame, not the preview.

        Live view cannot resolve whether you are inside a +/-11 mm depth
        of field; a full-resolution frame can.
        """
        import cv2
        import numpy as np

        from ..metrics.focus import exposure_stats, focus_map

        cam = cam_of(request)
        try:
            files = cam.capture_with_retry(0, 1)
            data = cam.read_file(files[0].file_id)
        except CameraError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(
                status_code=503,
                detail="could not decode the captured frame -- if this is a RAW "
                       "file the node needs a decoder installed")
        regions = [{"name": r.name, "score": round(r.score, 4)}
                   for r in focus_map(img)]
        small = cv2.resize(img, (640, int(640 * img.shape[0] / img.shape[1])))
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        import base64
        return {
            "camera_id": cam.camera_id,
            "size": [img.shape[1], img.shape[0]],
            "regions": regions,
            "exposure": exposure_stats(img),
            "jpeg_b64": base64.b64encode(buf.tobytes()).decode() if ok else None,
        }

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
        except StagingFull as e:
            # 507, not 503: the camera is fine and retrying will not help.
            # A node that answers "service unavailable" to a full disk sends
            # the operator back to the camera, which is the wrong place.
            # Must precede the CameraError arm -- StagingFull is a subclass.
            raise HTTPException(status_code=507, detail=str(e)) from e
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
