"""
Camera backend interface.

The whole point of this abstraction is that `mock` and `gphoto2` are
interchangeable.  Everything upstream -- the HTTP API, the orchestrator,
the pipeline -- is written against this interface and never learns which
one it is talking to.  Tomorrow, when the body arrives, one environment
variable changes and nothing else does.
"""

from __future__ import annotations

import abc
import threading
import time
from dataclasses import dataclass, field, asdict


class CameraError(RuntimeError):
    """Anything the camera did that it should not have."""


class CameraBusy(CameraError):
    pass


class CameraDisconnected(CameraError):
    """
    The PTP session dropped.

    The A6000 does this when idle.  It is not exceptional -- it is
    Tuesday.  Every backend must be able to recover from it without
    losing the session.
    """


class ConfigUnsupported(CameraError):
    """
    The body does not expose this setting at all.

    Distinct from "the body rejected the value", and the distinction
    matters: an absent key is a permanent property of the hardware to be
    reported once, while a rejected value is usually a fixable mistake
    (wrong mode dial, out-of-range choice).  Collapsing the two is how a
    node ends up claiming a setting it never applied.

    Measured on ILCE-6000: `capturetarget` is absent from the config tree
    entirely -- it is not merely defaulted.  See
    `first-light-measurements.md`.
    """


class StagingFull(CameraError):
    """
    The staging area cannot hold the frames this request would produce.

    Deliberately not a camera fault: the body is healthy and the fix is
    disk, not hardware.  Reported separately because the two demand
    opposite responses from an operator, and because a node that answers
    "capture failed" to a full filesystem sends people back to the camera
    -- which is exactly the wrong place.

    Raised *before* the shutter fires wherever the size is knowable.  A
    shutter actuation is a consumable: roughly 100k on an A6000, a few
    hundred per book.  Spending one on a frame that provably cannot be
    stored is waste with nothing to show for it.
    """


@dataclass
class CameraSettings:
    iso: int = 200
    shutter: str = "1/125"
    aperture: str = "8.0"
    #: *Requested* write destination: "card" or "internal".  This is a
    #: request, not a guarantee -- older bodies do not expose the setting
    #: at all and always stream to the host.  Read
    #: `CameraStatus.effective_capture_target` for what actually happens.
    capture_target: str = "card"
    image_format: str = "RAW"

    def merged(self, **kwargs) -> "CameraSettings":
        d = asdict(self)
        d.update({k: v for k, v in kwargs.items() if v is not None})
        return CameraSettings(**d)


@dataclass
class CapturedFile:
    file_id: str
    camera_id: str
    seq: int
    frame_index: int
    size_bytes: int
    #: Seconds from the capture command to the file being readable.
    latency_s: float
    content_type: str = "image/x-sony-arw"
    path: str | None = None


@dataclass
class CameraStatus:
    camera_id: str
    backend: str
    connected: bool
    model: str = ""
    settings: CameraSettings = field(default_factory=CameraSettings)
    frames_captured: int = 0
    last_error: str = ""
    busy: bool = False
    tile_index: int = 0
    #: What the body actually does with a frame: "card", "internal", or
    #: "" when not yet probed.  On a body with no `capturetarget` key this
    #: reads "internal" no matter what was requested.
    effective_capture_target: str = ""
    #: Setting names this body does not expose.  Never silently empty by
    #: accident -- a backend that cannot probe leaves it empty and says so
    #: via `last_error`.
    unsupported_settings: list[str] = field(default_factory=list)
    #: Headroom left in the staging area, in whole MB, or -1 where the
    #: backend has no staging area to measure.  Present so the operator
    #: learns about a filling disk from `/status` rather than from a
    #: capture that fails at page 300 -- on `scanner-node-0` the staging
    #: area is a tmpfs, so the host's own `df /` says nothing useful.
    staging_free_mb: int = -1


class CameraBackend(abc.ABC):
    """One physical (or simulated) camera on one node."""

    backend_name = "abstract"

    def __init__(self, camera_id: str, tile_index: int = 0):
        self.camera_id = camera_id
        self.tile_index = tile_index
        self.settings = CameraSettings()
        self.frames_captured = 0
        self.last_error = ""
        self.effective_capture_target = ""
        self.unsupported_settings: list[str] = []
        #: Largest frame this backend has actually written.  Used to size
        #: the staging preflight from measurement rather than from a guess.
        self._peak_frame_bytes = 0
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def connected(self) -> bool: ...

    def reconnect(self, attempts: int = 3, backoff_s: float = 0.5) -> None:
        """
        Recover a dropped session.

        Exponential backoff because hammering a camera that is still
        enumerating on the USB bus just extends the outage.
        """
        last: Exception | None = None
        for i in range(attempts):
            try:
                self.disconnect()
            except Exception:
                pass
            try:
                self.connect()
                return
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(backoff_s * (2**i))
        raise CameraDisconnected(
            f"{self.camera_id}: reconnect failed after {attempts} attempts: {last}"
        )

    # -- operations --------------------------------------------------------

    @abc.abstractmethod
    def model(self) -> str: ...

    @abc.abstractmethod
    def apply_settings(self, settings: CameraSettings) -> None: ...

    @abc.abstractmethod
    def capture(self, seq: int, frames: int = 1) -> list[CapturedFile]: ...

    @abc.abstractmethod
    def read_file(self, file_id: str) -> bytes: ...

    @abc.abstractmethod
    def preview(self) -> bytes:
        """A small JPEG for framing.  Never used for measurement."""

    def config_choices(self, name: str) -> list[str]:
        """
        The values this body will accept for a setting, in its own words.

        Needed because a computed exposure has to be snapped to a shutter
        speed the camera actually offers, and that list differs by model.
        Returning [] means "unknown", never "none available" -- a caller
        that cannot tell those apart will silently pick a wrong value.
        """
        return []

    # -- introspection -----------------------------------------------------

    def status(self) -> CameraStatus:
        return CameraStatus(
            camera_id=self.camera_id,
            backend=self.backend_name,
            connected=self.connected,
            model=self.model() if self.connected else "",
            settings=self.settings,
            frames_captured=self.frames_captured,
            last_error=self.last_error,
            busy=self._lock.locked(),
            tile_index=self.tile_index,
            effective_capture_target=self.effective_capture_target,
            unsupported_settings=list(self.unsupported_settings),
            staging_free_mb=(
                -1 if self.staging_free_bytes() < 0
                else self.staging_free_bytes() // (1024 * 1024)
            ),
        )

    # -- staging -----------------------------------------------------------

    #: Bytes to assume for a frame before one has ever been measured.
    #: 25 MiB: an ILCE-6000 compressed ARW is 24,526,080 B.
    NOMINAL_FRAME_BYTES = 25 * 1024 * 1024

    #: Spare room demanded on top of the frames being requested, so the
    #: node refuses while there is still space to write a diagnostic, not
    #: at the exact moment the filesystem stops accepting anything.
    STAGING_MARGIN_BYTES = 64 * 1024 * 1024

    def staging_free_bytes(self) -> int:
        """
        Free space where captured frames are staged, or -1 if not applicable.

        Backends that never write frames to disk leave this at -1 and the
        preflight below becomes a no-op.
        """
        return -1

    def require_staging_space(self, frames: int) -> None:
        """
        Refuse now, before the shutter moves, if the frames cannot be stored.

        Called by backends at the top of `capture()`.  The estimate uses the
        largest frame actually seen on this backend, falling back to
        NOMINAL_FRAME_BYTES, so it adapts to whatever the body really
        produces instead of trusting a constant.
        """
        free = self.staging_free_bytes()
        if free < 0:
            return
        need = frames * max(self._peak_frame_bytes, self.NOMINAL_FRAME_BYTES)
        need += self.STAGING_MARGIN_BYTES
        if free < need:
            raise StagingFull(
                f"{self.camera_id}: staging has {free // (1024 * 1024)} MB free, "
                f"{need // (1024 * 1024)} MB needed for {frames} frame(s) plus margin. "
                f"No shutter actuation was spent.  The frames already staged have "
                f"not been collected -- the orchestrator must fetch them, or they "
                f"must be dropped, before capture can continue."
            )

    def capture_with_retry(
        self, seq: int, frames: int = 1, attempts: int = 2
    ) -> list[CapturedFile]:
        """Capture, reconnecting once if the session dropped mid-command."""
        for i in range(attempts):
            try:
                return self.capture(seq, frames)
            except CameraDisconnected as e:
                self.last_error = str(e)
                if i == attempts - 1:
                    raise
                self.reconnect()
        raise CameraError("unreachable")
