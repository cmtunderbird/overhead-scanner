"""
Real camera backend: libgphoto2 via python-gphoto2.

Bindings, not the `gphoto2` command-line tool -- but see point 2: the
original reason given for that choice turned out to be exactly backwards
on real hardware, and the session model here reflects what was measured
rather than what was assumed.

Four things will cost you an evening each if you skip them:

1. Three different causes produce the identical message "Could not claim
   the USB device", and the obvious diagnosis is wrong two times out of
   three:

     - `gvfs-gphoto2-volume-monitor` grabs the device on a desktop Linux.
       Pi OS **Lite** has no desktop and therefore no gvfs, which is
       precisely why the blueprint specifies Lite.  On a desktop distro:
           systemctl --user mask gvfs-gphoto2-volume-monitor
     - `uaccess` grants a device ACL only to a user with an *active local
       seat*.  Over SSH there is none, so the ACL is never applied.
     - WSL has no seat either, for the same reason.

   In the last two the fix is membership of `plugdev`, not gvfs.

2. **A PTP session survives roughly one capture.**  Measured on the
   ILCE-6000: one persistent session managed 0 of 3 captures, while a
   fresh session per frame managed 3 of 3 at 3177 / 3098 / 3021 ms.
   Nothing physical changed between the two runs.  See
   `ptp-session-lifetime.md`.

   This inverts the reasoning that chose bindings over the CLI --
   *"every CLI invocation re-initialises the USB session, which costs
   about a second"*.  That re-initialisation is not overhead; it is the
   thing that makes capture work, and the feared cost does not appear in
   the numbers.  Bindings are still right; the session lifetime was not.
   Hence `session_per_capture=True` by default.

   The session also drops when idle.  `capture_with_retry` in the base
   class recovers -- but only for codes `_session_lost_codes()` reports,
   which is why `GP_ERROR` (-1) had to be added to it.

3. **`capturetarget` does not exist on every body.**  On the ILCE-6000 it
   is absent from the PTP config tree entirely -- not defaulted, absent.
   The body streams each frame to the host and nothing is written to the
   card.  Measured, see `first-light-measurements.md`:

       New file is in location /capt_DSC00001.ARW on the camera
       Saving file as capt1.ARW
       Deleting file /capt_DSC00001.ARW on the camera

   Card file count before: 0.  After: 0.

   The consequence is not cosmetic.  With no card copy there is no buffer
   to absorb a burst, so transfer time is strictly serial with capture,
   and `keep_on_camera` becomes actively harmful: the frame sits in the
   body's volatile storage and accumulates until capture fails.  This
   backend therefore *probes* rather than assumes, and resolves
   `keep_on_camera` from what the hardware can actually do.

4. `expprogram` is **read-only** over PTP.  The mode dial is the one
   setting that genuinely must be set by hand, and Sony bodies refuse
   aperture and shutter over PTP unless it is on M.

On the camera itself: USB Connection -> PC Remote, Auto Review -> Off,
mode dial -> M, Pre-AF -> Off, Focus -> DMF.
"""

from __future__ import annotations

import pathlib
import shutil
import tempfile
import time

from .base import (
    CameraBackend,
    CameraDisconnected,
    CameraError,
    CameraSettings,
    CapturedFile,
    ConfigUnsupported,
    StagingFull,
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

    #: Settings this backend will try to apply, in order.  `expprogram` is
    #: deliberately absent: it is read-only on Sony bodies.
    SETTING_KEYS = ("iso", "shutterspeed", "f-number")

    def __init__(self, camera_id: str = "cam0", tile_index: int = 0,
                 *, keep_on_camera: bool | None = None,
                 session_per_capture: bool = True):
        super().__init__(camera_id, tile_index)
        self._camera = None
        self._gp = None
        self._config_paths: dict[str, str] = {}
        #: Open a fresh PTP session for every frame. Measured on ILCE-6000:
        #: a persistent session manages exactly one capture and the second
        #: fails. See `ptp-session-lifetime.md`. Set False only to
        #: reproduce that.
        self.session_per_capture = session_per_capture
        #: Frames downloaded during capture, keyed by file_id. Needed
        #: because a body with no card keeps the frame in volatile storage
        #: tied to the session -- close the session and it is gone, so a
        #: deferred read_file() would find nothing.
        self._cache: dict[str, pathlib.Path] = {}
        self._cache_dir: pathlib.Path | None = None
        #: False when the config tree could not be read at all.  Without
        #: this, "probe failed" and "body supports nothing" are the same
        #: empty dict, and the more serious diagnosis loses.
        self._probe_ok = False
        #: None means "decide from what the body supports" -- the only
        #: safe default, because leaving frames on a body that has no card
        #: target fills its volatile storage.  True/False force it.
        self.keep_on_camera = keep_on_camera
        self._keep_requested = keep_on_camera

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        self._open_session()
        self.last_error = ""
        self._config_paths = self._probe_config()
        self._resolve_capture_target()
        if self._cache_dir is None:
            self._cache_dir = pathlib.Path(
                tempfile.mkdtemp(prefix=f"scanner-{self.camera_id}-"))

    def _open_session(self) -> None:
        """
        Open a PTP session. Init only -- deliberately no config probe.

        `connect()` probes once; every later reopen reuses that map. On a
        body that manages roughly one capture per session, spending extra
        PTP round-trips re-discovering capabilities we already know is
        exactly the wrong trade.
        """
        try:
            import gphoto2 as gp
        except ImportError as e:  # pragma: no cover
            raise CameraError(
                "python-gphoto2 is not installed.\n"
                "  pip install gphoto2\n"
                "The manylinux wheel bundles libgphoto2, so no compiler and no\n"
                "libgphoto2-dev are needed.  Do install the distro package for\n"
                "its udev rules, which the wheel does not ship:\n"
                "  sudo apt install -y libgphoto2-6t64   # libgphoto2-6 pre-24.04"
            ) from e

        self._gp = gp
        cam = gp.Camera()
        try:
            cam.init()
        except gp.GPhoto2Error as e:
            if e.code == gp.GP_ERROR_MODEL_NOT_FOUND:
                raise CameraDisconnected(
                    f"{self.camera_id}: no camera found.  Check the cable "
                    f"carries data (charge-only micro-USB leads enumerate "
                    f"nothing at all), and that the body is on and set to "
                    f"PC Remote."
                ) from e
            if e.code == gp.GP_ERROR_IO_USB_CLAIM:
                raise CameraError(
                    f"{self.camera_id}: another process holds the USB device, "
                    f"or you lack permission.  Three causes, one message:\n"
                    f"  desktop Linux -> gvfs:\n"
                    f"    systemctl --user mask gvfs-gphoto2-volume-monitor\n"
                    f"    pkill -f gvfs-gphoto2\n"
                    f"  over SSH or under WSL -> no local seat, so uaccess\n"
                    f"  grants nothing.  Join plugdev instead:\n"
                    f"    sudo usermod -aG plugdev $USER   # then log in again"
                ) from e
            raise CameraError(f"{self.camera_id}: {e}") from e

        self._camera = cam

    def _reopen(self) -> None:
        """
        Close and reopen the PTP session, keeping the probed capability
        map. This is the fix for the one-capture-per-session limit.
        """
        paths, ok = self._config_paths, self._probe_ok
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._open_session()
        self._config_paths, self._probe_ok = paths, ok

    def disconnect(self) -> None:
        if self._camera is not None:
            try:
                self._camera.exit()
            except Exception:  # noqa: BLE001
                pass
        self._camera = None
        self._config_paths = {}

    @property
    def connected(self) -> bool:
        return self._camera is not None

    # -- capability probing ------------------------------------------------

    def _probe_config(self) -> dict[str, str]:
        """
        Map every leaf setting name to its full path, once per session.

        Probing beats assuming: bodies differ by model *and* firmware, and
        an absent key is indistinguishable from a rejected value unless
        you look.
        """
        cam, gp = self._require()

        def walk(node, prefix=""):
            name = node.get_name()
            path = f"{prefix}/{name}"
            try:
                n = node.count_children()
            except Exception:  # noqa: BLE001
                n = 0
            if n == 0:
                yield name, path
            else:
                for i in range(n):
                    yield from walk(node.get_child(i), path)

        try:
            paths = dict(walk(cam.get_config()))
        except Exception as e:  # noqa: BLE001
            # Do not pretend we probed.  An empty map with no explanation
            # would read as "this body supports nothing".
            self._probe_ok = False
            self.last_error = f"config probe failed: {e}"
            return {}
        self._probe_ok = True
        return paths

    def has_config(self, name: str) -> bool:
        return name in self._config_paths

    def _resolve_capture_target(self) -> None:
        """
        Decide where frames land, and whether they must be deleted.

        A body with no `capturetarget` key streams to the host and keeps
        the frame in volatile storage until it is removed.  Keeping files
        on such a body is not a preference, it is a slow leak.
        """
        wanted = "card" if self.settings.capture_target == "card" else "internal"

        if not self._probe_ok:
            # We do not know what this body supports.  Say nothing about
            # capabilities, keep the probe failure as the reported error,
            # and delete after download because an unconfirmed card is not
            # a card.
            self.effective_capture_target = ""
            self.keep_on_camera = bool(self._keep_requested)
            return

        if not self.has_config("capturetarget"):
            self.effective_capture_target = "internal"
            if "capturetarget" not in self.unsupported_settings:
                self.unsupported_settings.append("capturetarget")
            # Honour an explicit override, but default to deleting.
            self.keep_on_camera = bool(self._keep_requested)
            if wanted == "card":
                self.last_error = (
                    f"{self.camera_id}: body has no capturetarget setting; "
                    f"frames stream to the host and are not written to the "
                    f"card.  Transfer is serial with capture."
                )
            return

        try:
            self._set_config(
                "capturetarget",
                "Memory card" if wanted == "card" else "Internal RAM",
            )
            self.effective_capture_target = wanted
        except CameraError as e:
            self.effective_capture_target = "internal"
            self.last_error = str(e)
        if self._keep_requested is None:
            self.keep_on_camera = self.effective_capture_target == "card"

    # -- config ------------------------------------------------------------

    def _require(self):
        if self._camera is None:
            raise CameraDisconnected(f"{self.camera_id}: not connected")
        return self._camera, self._gp

    def _set_config(self, name: str, value) -> None:
        cam, gp = self._require()
        if self._config_paths and name not in self._config_paths:
            raise ConfigUnsupported(
                f"{self.camera_id}: this body has no '{name}' setting"
            )
        try:
            cfg = cam.get_config()
            node = cfg.get_child_by_name(name)
            node.set_value(value)
            cam.set_config(cfg)
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: cannot set {name}={value}: {e}") from e

    def _get_config(self, name: str):
        cam, gp = self._require()
        try:
            return cam.get_config().get_child_by_name(name).get_value()
        except Exception as e:  # noqa: BLE001
            raise CameraError(f"{self.camera_id}: cannot read {name}: {e}") from e

    def config_choices(self, name: str) -> list[str]:
        cam, gp = self._require()
        if self._config_paths and name not in self._config_paths:
            return []
        try:
            node = cam.get_config().get_child_by_name(name)
            return [str(c) for c in node.get_choices()]
        except Exception:  # noqa: BLE001
            return []

    def model(self) -> str:
        cam, gp = self._require()
        try:
            cfg = cam.get_config()
            return str(cfg.get_child_by_name("cameramodel").get_value())
        except Exception:  # noqa: BLE001
            return "unknown"

    def apply_settings(self, settings: CameraSettings) -> None:
        """
        Apply settings and *verify them by read-back*.

        The previous version recorded `self.settings = settings`
        unconditionally, so `/status` reported values the body had never
        accepted -- a measurement that looks healthy while measuring
        nothing.  Only what verifiably took is recorded.
        """
        self._require()
        wanted = {
            "iso": str(settings.iso),
            "shutterspeed": settings.shutter,
            "f-number": settings.aperture,
        }
        applied: dict[str, str] = {}
        absent: list[str] = []
        rejected: list[str] = []

        for name in self.SETTING_KEYS:
            value = wanted[name]
            try:
                self._set_config(name, value)
            except ConfigUnsupported:
                absent.append(name)
                continue
            except CameraError as e:
                # Sony bodies refuse aperture/shutter over PTP unless the
                # mode dial is on M.  Report it, do not pretend it worked.
                rejected.append(f"{name}: {e}")
                continue
            try:
                got = str(self._get_config(name))
            except CameraError:
                got = ""
            if got and got.strip() != value.strip():
                rejected.append(f"{name}: asked {value!r}, body reports {got!r}")
            else:
                applied[name] = value

        for name in absent:
            if name not in self.unsupported_settings:
                self.unsupported_settings.append(name)

        # Record only what actually took.
        self.settings = self.settings.merged(
            iso=int(applied["iso"]) if "iso" in applied else None,
            shutter=applied.get("shutterspeed"),
            aperture=applied.get("f-number"),
            image_format=settings.image_format,
        )
        if settings.capture_target != self.settings.capture_target:
            self.settings = self.settings.merged(
                capture_target=settings.capture_target)
            self._resolve_capture_target()

        problems = rejected + [f"{n}: unsupported by this body" for n in absent]
        self.last_error = "; ".join(problems) if problems else ""

    # -- capture -----------------------------------------------------------

    def _session_lost_codes(self) -> tuple[int, ...]:
        """
        Error codes that mean "this session is finished, open a new one".

        `GP_ERROR` (-1, "Unspecified error") belongs here: it is the
        *observed* death on the ILCE-6000 after one capture. Without it,
        `capture_with_retry` in the base class never fires for the only
        failure that actually occurs.
        """
        gp = self._gp
        return (
            getattr(gp, "GP_ERROR_IO", -7),
            getattr(gp, "GP_ERROR_IO_USB_FIND", -52),
            getattr(gp, "GP_ERROR", -1),
        )

    def staging_free_bytes(self) -> int:
        if self._cache_dir is None:
            return -1
        try:
            return shutil.disk_usage(self._cache_dir).free
        except OSError:
            return -1

    def capture(self, seq: int, frames: int = 1) -> list[CapturedFile]:
        self._require()
        # Before the shutter, not after: a full staging area is knowable in
        # advance, and an actuation spent on a frame that cannot be written
        # is a consumable burned for nothing.
        self.require_staging_space(frames)
        out: list[CapturedFile] = []
        with self._lock:
            for f in range(frames):
                if self.session_per_capture:
                    self._reopen()
                out.append(self._shoot(seq, f))
        return out

    def _shoot(self, seq: int, frame_index: int) -> CapturedFile:
        cam, gp = self._require()
        t0 = time.perf_counter()
        try:
            path = cam.capture(gp.GP_CAPTURE_IMAGE)
        except gp.GPhoto2Error as e:
            if e.code in self._session_lost_codes():
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

        # A body with no card holds the frame only for the life of THIS
        # session. Closing the session discards it, so a deferred
        # read_file() would find nothing. Pull it now.
        if not self.keep_on_camera:
            # The frame exists only inside this session, so every failure
            # below loses it permanently.  None of them may be swallowed:
            # a capture that did not secure its frame is a failed capture,
            # however healthy the shutter sounded.
            data = self._download(path.folder, path.name)
            size = len(data)
            self._peak_frame_bytes = max(self._peak_frame_bytes, size)
            if self._cache_dir is not None:
                dest = self._cache_dir / f"{seq:06d}-{frame_index}-{path.name}"
                try:
                    dest.write_bytes(data)
                except OSError as e:
                    free = self.staging_free_bytes()
                    raise StagingFull(
                        f"{self.camera_id}: staging write failed after the frame "
                        f"was already downloaded ({size} B, "
                        f"{free // (1024 * 1024) if free >= 0 else '?'} MB free): {e}. "
                        f"The actuation and the transfer are both spent."
                    ) from e
                self._cache[fid] = dest
            try:
                cam.file_delete(path.folder, path.name)
            except Exception:  # noqa: BLE001
                pass

        self.frames_captured += 1
        return CapturedFile(
            file_id=fid,
            camera_id=self.camera_id,
            seq=seq,
            frame_index=frame_index,
            size_bytes=size,
            latency_s=round(time.perf_counter() - t0, 3),
            path=fid,
        )

    def _download(self, folder: str, name: str) -> bytes:
        cam, gp = self._require()
        try:
            cam_file = cam.file_get(folder or "/", name, gp.GP_FILE_TYPE_NORMAL)
            return memoryview(cam_file.get_data_and_size()).tobytes()
        except gp.GPhoto2Error as e:
            raise CameraError(
                f"{self.camera_id}: download failed for {folder}/{name}: {e}") from e

    def read_file(self, file_id: str) -> bytes:
        # Frames pulled during capture are served from the local cache.
        # On a body with no card this is the only copy that still exists.
        cached = self._cache.get(file_id)
        if cached is not None and cached.exists():
            return cached.read_bytes()

        cam, gp = self._require()
        folder, _, name = file_id.rpartition("/")
        data = self._download(folder, name)
        if not self.keep_on_camera:
            try:
                cam.file_delete(folder or "/", name)
            except Exception:  # noqa: BLE001
                pass
        return data

    def release_cache(self) -> None:
        """Drop cached frames once the orchestrator has taken them."""
        for p in self._cache.values():
            try:
                p.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
        self._cache.clear()
        if self._cache_dir is not None:
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            self._cache_dir = None

    def preview(self) -> bytes:
        cam, gp = self._require()
        try:
            cam_file = cam.capture_preview()
            return memoryview(cam_file.get_data_and_size()).tobytes()
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: preview failed: {e}") from e
