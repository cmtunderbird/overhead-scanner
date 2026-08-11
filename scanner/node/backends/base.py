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
