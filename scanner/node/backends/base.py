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


class DuplicateFrame(CameraError):
    """
    A capture returned a frame byte-identical to the one before it.

    Two independent mechanisms produce this, and both are silent:

    * **The body's volatile buffer keeps a frame from a failed capture.**
      libgphoto2's own source says so, above the Sony recovery path:
      *"the image is immediately available as object in RAM 0x8001 so
      function will start download during exposition and we get another
      image than supposed.  It may even accumulate so we will get images
      from history.  Camera on-off does not delete RAM.  Just USB
      reconnection helps."*
    * **python-gphoto2 #65**: after `exit()`/`init()`, `capture()` can
      return the *same* `CameraFilePath` while the body is genuinely
      shooting new frames.

    Either way the node hands the orchestrator page N+1's slot filled with
    page N's pixels.  Nothing raises, nothing logs, and the defect is
    discoverable only by reading the finished book -- a duplicated page and
    a missing one, several hundred pages in.

    So this is fatal by design.  A refused capture costs one actuation; a
    silently duplicated frame costs the run.  On real hardware two frames
    are never bit-identical -- sensor noise alone guarantees it -- so a
    match here is evidence of the hazard, not a coincidence.
    """


class OpticalStateChanged(CameraError):
    """
    The lens is not where it was when the rig was calibrated.

    Raised on the focal length read from a captured frame's EXIF differing
    from the one recorded at calibration.  The failure this exists to catch
    is specific and cheap to hit: the **E PZ 16-50 resets to 16 mm on every
    power-off**, so a battery swap, a flat cell, or an accidental power
    cycle silently invalidates magnification, DPI and the distortion
    profile -- while the node carries on capturing perfectly good frames of
    the wrong thing.

    A focal length is four bytes of EXIF we were already parsing the file
    for.  Checking it turns a defect that is silent, intermittent and
    corrupts data into one that stops the run on the first frame.
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
    #: True when staged frames will not survive a restart of the service.
    #: Reported rather than hidden: an operator has to be able to read that
    #: off /status, not discover it after a reboot.
    staging_volatile: bool = False
    #: Frames captured and not yet claimed by the orchestrator.  This is
    #: the number that fills; `staging_free_mb` is only its consequence.
    staged_frames: int = 0
    #: Frames dropped by the cap in `release_oldest_if_over_cap`.  A cap
    #: that evicts silently reads as "nothing was lost", so this is
    #: published whether or not anyone asks.
    frames_evicted: int = 0
    #: Frames adopted from a previous run of this process at startup.
    #: Non-zero means the service restarted while the orchestrator still
    #: had frames outstanding, and they survived.
    frames_recovered: int = 0
    #: Deepest recovery this backend has needed: reopened PTP sessions and
    #: USB re-enumerations.  A rising re-enumeration count on a node that
    #: is otherwise healthy is a cable or a power problem, not software.
    session_reopens: int = 0
    usb_resets: int = 0
    #: Focal length from the last frame's EXIF, and the value the rig was
    #: calibrated at.  They differing is the whole point -- see
    #: `OpticalStateChanged`.
    focal_length_mm: float = 0.0
    expected_focal_length_mm: float = 0.0
    #: Non-empty when the optics moved.  Separate from `last_error`
    #: because it survives a successful capture: the frames keep coming,
    #: they are simply no longer the frames that were calibrated for.
    optical_alarm: str = ""


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
        self.frames_evicted = 0
        self.frames_recovered = 0
        self.session_reopens = 0
        self.usb_resets = 0
        self.focal_length_mm = 0.0
        #: 0.0 means "not calibrated yet, so nothing to compare against".
        #: Set it once the zoom is taped and the rig is calibrated.
        self.expected_focal_length_mm = 0.0
        self.optical_alarm = ""

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
            staging_volatile=bool(getattr(self, "staging_volatile", False)),
            staged_frames=len(self.staged_frames()),
            frames_evicted=self.frames_evicted,
            frames_recovered=self.frames_recovered,
            session_reopens=self.session_reopens,
            usb_resets=self.usb_resets,
            focal_length_mm=self.focal_length_mm,
            expected_focal_length_mm=self.expected_focal_length_mm,
            optical_alarm=self.optical_alarm,
        )

    # -- staging -----------------------------------------------------------

    #: Bytes to assume for a frame before one has ever been measured.
    #: 25 MiB: an ILCE-6000 compressed ARW is 24,526,080 B.
    NOMINAL_FRAME_BYTES = 25 * 1024 * 1024

    #: Spare room demanded on top of the frames being requested, so the
    #: node refuses while there is still space to write a diagnostic, not
    #: at the exact moment the filesystem stops accepting anything.
    STAGING_MARGIN_BYTES = 64 * 1024 * 1024

    def read_settings(self) -> dict[str, str]:
        """
        Values read back from the hardware right now, or {} if unavailable.

        Keyed by the body's own config names.  Backends that cannot ask the
        hardware return {} and callers must treat that as "unknown", never
        as "matches".
        """
        return {}

    def staging_free_bytes(self) -> int:
        """
        Free space where captured frames are staged, or -1 if not applicable.

        Backends that never write frames to disk leave this at -1 and the
        preflight below becomes a no-op.
        """
        return -1

    # -- frame claiming ----------------------------------------------------
    #
    # The contract, and why it is this one.
    #
    # Until now `release_cache()` existed with no caller and no route that
    # could give it one, so staging filled at roughly 320 frames and the
    # node stopped -- honestly, since PR #6, but it still stopped.  What
    # was missing was a way for the orchestrator to say *"I have these
    # frames, you may forget them"*.
    #
    # libgphoto2 has already answered this question for itself, and its
    # answer is worth copying rather than re-deriving.  `gphoto2
    # --capture-image-and-download` **deletes by default** (`--keep` opts
    # out), and on Sony that delete never reaches the camera at all --
    # `delete_file_func()` returns `GP_OK` immediately for any file named
    # `capt*` and simply drops the host-side copy.  Behind it sits a
    # bounded LRU, `PICTURES_TO_KEEP`, defaulting to 2 and tunable at
    # runtime.  So: an explicit release call whose meaning is host-side
    # eviction, with a cap behind it as a backstop.
    #
    # Two deliberate departures from that design:
    #
    # * **Read-once on GET was rejected.**  It is the tempting option --
    #   no new route, no orchestrator change -- and it makes a retried or
    #   double-read GET destroy a page silently.  It also makes `/files`
    #   useless for debugging and impossible for a second consumer.  A
    #   release must be something the caller *asks for*, not a side effect
    #   of looking.
    # * **Metadata is freed with the payload.**  libgphoto2's own LRU
    #   frees the pixel data and leaves the `CameraFilesystemFile` node --
    #   name, info, list linkage -- so its directory listing grows without
    #   bound over a long tether while the bytes are reclaimed.  That is
    #   its bug, not its design, and copying it would move the ceiling
    #   from 320 frames to 100,000 rather than removing it.

    #: Frames to hold before the oldest is evicted.  A backstop, not a
    #: working limit: at 24.5 MB a frame this is ~5.9 GB, comfortably
    #: inside the 8 GB staging area, and an orchestrator that claims its
    #: frames will never reach it.  One that has crashed will, and then
    #: dropping the oldest frame beats refusing every new one.
    MAX_STAGED_FRAMES = 240

    def staged_frames(self) -> list[str]:
        """
        File ids held for the orchestrator, oldest first.

        Backends with nothing to stage return [] and every method below
        becomes a no-op.
        """
        return []

    def release_file(self, file_id: str) -> bool:
        """
        Forget one frame.  True if it was there, False if it was not.

        Idempotent on purpose.  An orchestrator that retries a release
        after a network timeout must not be told it did something wrong,
        and "already gone" is the desired state either way.
        """
        return False

    def release_all(self) -> int:
        """Forget every staged frame.  Returns how many there were."""
        ids = list(self.staged_frames())
        for fid in ids:
            self.release_file(fid)
        return len(ids)

    def enforce_frame_cap(self) -> int:
        """
        Drop the oldest frames until the cap is met.  Returns how many went.

        Never silent: every eviction increments `frames_evicted`, which
        `/status` publishes.  A cap that quietly discards data reads
        exactly like a cap that was never reached.
        """
        dropped = 0
        ids = self.staged_frames()
        while len(ids) > self.MAX_STAGED_FRAMES:
            self.release_file(ids[0])
            self.frames_evicted += 1
            dropped += 1
            ids = self.staged_frames()
        return dropped

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

    def recover(self) -> None:
        """
        Get the camera working again, escalating as far as this backend can.

        The base implementation is just `reconnect()`.  A backend with a
        deeper tier overrides this.

        **It exists as a named seam, and that is the whole point.**  PR #9
        added `usb_reset()` and a `recover()` that escalates to it, and
        then left both unreachable, because `capture_with_retry` went on
        calling `reconnect()` directly.  A recovery tier nothing invokes
        looks exactly like protection and provides none -- the same defect
        as `release_cache()` sitting there with no caller, shipped in the
        very change that fixed it.

        Every recovery path routes through here so that adding a tier is
        enough to make it reachable.
        """
        self.reconnect()

    def capture_with_retry(
        self, seq: int, frames: int = 1, attempts: int = 2
    ) -> list[CapturedFile]:
        """Capture, recovering once if the link dropped mid-command."""
        for i in range(attempts):
            try:
                return self.capture(seq, frames)
            except CameraDisconnected as e:
                self.last_error = str(e)
                if i == attempts - 1:
                    raise
                self.recover()
        raise CameraError("unreachable")

    def preview_with_retry(self, attempts: int = 2) -> bytes:
        """
        Live view, with the same recovery `capture()` has had all along.

        Measured on `scanner-node-0`, 2026-08-12: after ~7 hours idle the
        body stopped answering and `/preview` returned
        `[-7] I/O problem` -- a code `_session_lost_codes()` already
        classifies as "this session is finished, open a new one".  The
        capture path recovered from it and the preview path did not,
        because `preview()` called the camera directly.

        The asymmetry is the bug.  An operator framing a page is the most
        likely person to meet an idle session, since framing is what you
        do *after* leaving the rig alone.
        """
        for i in range(attempts):
            try:
                return self.preview()
            except CameraDisconnected as e:
                self.last_error = str(e)
                if i == attempts - 1:
                    raise
                self.recover()
        raise CameraError("unreachable")
