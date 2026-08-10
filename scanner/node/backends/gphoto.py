"""
Real camera backend: libgphoto2 via python-gphoto2.

Bindings, not the `gphoto2` command-line tool.  Every CLI invocation
re-initialises the USB session, which costs about a second and makes
burst capture impossible.

Three things will cost you an evening each if you skip them:

1. `gvfs-gphoto2-volume-monitor` claims the camera the instant it
   enumerates, and every command then fails with "Could not claim the USB
   device".  Pi OS **Lite** has no desktop and therefore no gvfs, which
   is precisely why the blueprint specifies Lite.  On a desktop distro:
       systemctl --user mask gvfs-gphoto2-volume-monitor

2. The A6000 drops its PTP session when idle.  This is normal.  Reconnect
   on failure -- `capture_with_retry` in the base class does it for you.

3. `capturetarget` defaults vary by body and firmware, and silently
   decide whether files land in camera RAM or on the card.  That changes
   your entire timing model, so it is set explicitly here.

On the camera itself: USB Connection -> PC Remote, Auto Review -> Off,
mode dial -> M, Pre-AF -> Off, Focus -> DMF.
"""

from __future__ import annotations

import time

from .base import (
    CameraBackend,
    CameraDisconnected,
    CameraError,
    CameraSettings,
    CapturedFile,
)


def gphoto2_available() -> bool:
    try:
        import gphoto2  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class GPhotoCamera(CameraBackend):
    """A camera on the other end of a USB cable."""

    backend_name = "gphoto2"

    def __init__(self, camera_id: str = "cam0", tile_index: int = 0,
                 *, keep_on_camera: bool = True):
        super().__init__(camera_id, tile_index)
        self._camera = None
        self._gp = None
        #: True leaves files on the card and fetches them over PTP.  False
        #: deletes after download.
        self.keep_on_camera = keep_on_camera

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        try:
            import gphoto2 as gp
        except ImportError as e:  # pragma: no cover
            raise CameraError(
                "python-gphoto2 is not installed.  On the Pi:\n"
                "  sudo apt install -y libgphoto2-dev\n"
                "  pip install gphoto2"
            ) from e

        self._gp = gp
        cam = gp.Camera()
        try:
            cam.init()
        except gp.GPhoto2Error as e:
            if e.code == gp.GP_ERROR_MODEL_NOT_FOUND:
                raise CameraDisconnected(
                    f"{self.camera_id}: no camera found.  Check the cable, "
                    f"and that the body is on and set to PC Remote."
                ) from e
            if e.code == gp.GP_ERROR_IO_USB_CLAIM:
                raise CameraError(
                    f"{self.camera_id}: another process holds the USB device. "
                    f"This is almost always gvfs:\n"
                    f"  systemctl --user mask gvfs-gphoto2-volume-monitor\n"
                    f"  pkill -f gvfs-gphoto2"
                ) from e
            raise CameraError(f"{self.camera_id}: {e}") from e

        self._camera = cam
        self.last_error = ""
        try:
            self._set_config(
                "capturetarget",
                "Memory card" if self.settings.capture_target == "card" else "Internal RAM",
            )
        except CameraError:
            pass  # not every body exposes it; not fatal

    def disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.exit()
            except Exception:  # noqa: BLE001
                pass
        self._camera = None

    @property
    def connected(self) -> bool:
        return self._camera is not None

    # -- config ------------------------------------------------------------

    def _require(self):
        if self._camera is None:
            raise CameraDisconnected(f"{self.camera_id}: not connected")
        return self._camera, self._gp

    def _set_config(self, name: str, value) -> None:
        cam, gp = self._require()
        try:
            cfg = cam.get_config()
            node = cfg.get_child_by_name(name)
            node.set_value(value)
            cam.set_config(cfg)
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: cannot set {name}={value}: {e}") from e

    def model(self) -> str:
        cam, gp = self._require()
        try:
            cfg = cam.get_config()
            return str(cfg.get_child_by_name("cameramodel").get_value())
        except Exception:  # noqa: BLE001
            return "unknown"

    def apply_settings(self, settings: CameraSettings) -> None:
        self._require()
        for name, value in (
            ("iso", str(settings.iso)),
            ("shutterspeed", settings.shutter),
            ("f-number", settings.aperture),
        ):
            try:
                self._set_config(name, value)
            except CameraError as e:
                # Sony bodies refuse aperture/shutter over PTP unless the
                # mode dial is on M.  Report it, do not pretend it worked.
                self.last_error = str(e)
        self.settings = settings

    # -- capture -----------------------------------------------------------

    def capture(self, seq: int, frames: int = 1) -> list[CapturedFile]:
        cam, gp = self._require()
        out: list[CapturedFile] = []
        with self._lock:
            for f in range(frames):
                t0 = time.perf_counter()
                try:
                    path = cam.capture(gp.GP_CAPTURE_IMAGE)
                except gp.GPhoto2Error as e:
                    if e.code in (gp.GP_ERROR_IO, gp.GP_ERROR_IO_USB_FIND):
                        raise CameraDisconnected(
                            f"{self.camera_id}: session lost during capture: {e}"
                        ) from e
                    raise CameraError(f"{self.camera_id}: capture failed: {e}") from e

                fid = f"{path.folder.rstrip('/')}/{path.name}"
                try:
                    info = cam.file_get_info(path.folder, path.name)
                    size = int(info.file.size)
                except Exception:  # noqa: BLE001
                    size = -1

                self.frames_captured += 1
                out.append(
                    CapturedFile(
                        file_id=fid,
                        camera_id=self.camera_id,
                        seq=seq,
                        frame_index=f,
                        size_bytes=size,
                        latency_s=round(time.perf_counter() - t0, 3),
                        path=fid,
                    )
                )
        return out

    def read_file(self, file_id: str) -> bytes:
        cam, gp = self._require()
        folder, _, name = file_id.rpartition("/")
        try:
            cam_file = cam.file_get(folder or "/", name, gp.GP_FILE_TYPE_NORMAL)
            data = memoryview(cam_file.get_data_and_size()).tobytes()
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: download failed for {file_id}: {e}") from e
        if not self.keep_on_camera:
            try:
                cam.file_delete(folder or "/", name)
            except Exception:  # noqa: BLE001
                pass
        return data

    def preview(self) -> bytes:
        cam, gp = self._require()
        try:
            cam_file = cam.capture_preview()
            return memoryview(cam_file.get_data_and_size()).tobytes()
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: preview failed: {e}") from e
