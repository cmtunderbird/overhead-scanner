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

5. **Surviving an interruption is three separate problems**, and solving
   only the first leaves the other two silent:

     - *The link.*  The PTP session dies when idle and after roughly one
       capture.  `_reopen` handles that.  What it cannot handle is a body
       whose volatile buffer is holding a frame from a failed capture:
       libgphoto2's own source says *"Camera on-off does not delete RAM.
       Just USB reconnection helps."*  So the deepest recovery tier here
       is a USB re-enumeration, not a reconnect and not a power cycle --
       and a power cycle is specifically the thing that does not work.
     - *The frames.*  See `_resolve_staging_root`: the staging area used
       to be `/tmp`, which under `PrivateTmp=yes` is a tmpfs the service
       owns and `Restart=always` destroys.
     - *The optics.*  A power cycle -- a battery swap, a flat cell, a
       knocked barrel -- resets the E PZ 16-50 to 16 mm.  Nothing about
       capture notices, so the node goes on producing technically
       excellent frames at the wrong magnification, and the DPI and
       distortion calibration are quietly void.  Every frame's focal
       length is checked against the calibrated one for this reason.

On the camera itself: USB Connection -> PC Remote, Auto Review -> Off,
mode dial -> M, Pre-AF -> Off, **Focus -> MF** (not DMF), and **a card in
the body**.

Two of those five deserve their reasons written down, because both were
paid for:

* **MF, not DMF.**  `camera_sony_capture()` skips its entire focus-wait
  loop only when `FocusMode == 1`, which is Manual; DMF is not 1, so DMF
  re-enters the wait and costs up to a second a frame.  Worse, DMF holds
  an AF interlock that refuses the shutter outright -- recorded in
  `exposure-and-lighting.md` §3 as a capture that never happened.  Older
  advice recommending DMF (BYU's `a6000_ros`, 2018) was written against
  libgphoto2 2.5.21, where the focus wait ran unconditionally and capped
  at 1 s, so DMF cost nearly nothing.  On a current stack it is a
  liability.
* **A card in the body.**  Not for storage -- `capturetarget` is absent,
  nothing is written to it, and in PC Remote the card is not even visible
  over PTP.  It is what makes the body capture reliably at all.  See
  `node-open-defects.md`; a night was spent proving it the hard way.
"""

from __future__ import annotations

import hashlib
import math
import os
import pathlib
import re
import shutil
import tempfile
import time
import urllib.parse

from ...calib.exposure import parse_shutter

from .base import (
    CameraBackend,
    CameraDisconnected,
    CameraError,
    CameraSettings,
    CapturedFile,
    ConfigUnsupported,
    DuplicateFrame,
    OpticalStateChanged,
    StagingFull,
)


_F_PREFIX = re.compile(r"^\s*f\s*/?\s*", re.I)


def _aperture_number(raw) -> float:
    """5.6 from '5.6', 'f/5.6', 'F5.6' -- the body is not consistent."""
    return float(_F_PREFIX.sub("", str(raw)).strip())


def canonical_setting(name: str, raw) -> str:
    """
    One spelling per value, so a setting is not recorded twice two ways.

    The node's own notation is the bare number: `400`, `1/60`, `5.6`.
    """
    if name == "iso":
        return str(int(float(str(raw).strip())))
    if name == "f-number":
        return f"{_aperture_number(raw):g}"
    return str(raw).strip()


def same_setting(name: str, asked, got) -> bool:
    """
    Compare a requested value with the body's read-back **by value**.

    Measured on ILCE-6000, 2026-08-12: asked `f-number` = `'5.6'`, body
    reported `'f/5.6'`.  A string comparison calls that a rejection -- so a
    setting the camera had applied perfectly was recorded as failed,
    `/status` kept the previous aperture, and `last_error` raised an alarm
    about a body that had done exactly as it was told.  Confirmed from the
    captured frame's EXIF: `F Number: 5.6`.

    Shutter needs the same treatment for a worse reason: Sony reports 1/2 s
    as the unreduced `'5/10'`, so `'1/2' != '5/10'` as text while being the
    same exposure.  `parse_shutter` already knows this.
    """
    try:
        if name == "iso":
            return int(float(str(asked))) == int(float(str(got)))
        if name == "f-number":
            return abs(_aperture_number(asked) - _aperture_number(got)) < 1e-3
        if name == "shutterspeed":
            a, b = parse_shutter(asked), parse_shutter(got)
            if a <= 0 or b <= 0:
                return a == b
            # A hundredth of a stop: far tighter than any real difference
            # between adjacent shutter choices, loose enough for rounding.
            return abs(math.log2(a / b)) < 0.01
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    return str(asked).strip() == str(got).strip()


#: Where staged frames go when nothing else is configured.  systemd gives
#: the unit this directory via `StateDirectory=scanner`, which survives a
#: restart; the path is the documented location for exactly that.
DEFAULT_STAGING_ROOT = pathlib.Path("/var/lib/scanner/staging")


def _resolve_staging_root() -> tuple[pathlib.Path, bool]:
    """
    Pick the staging directory.  Returns (path, volatile).

    **Why this is not `tempfile.mkdtemp()` any more.**  The node runs under
    a unit with `PrivateTmp=yes` and `Restart=always`.  `PrivateTmp` hands
    the service its own tmpfs mounted at `/tmp`, torn down when the unit
    stops -- so every staged frame lived somewhere a restart erases.
    `Restart=always` then guarantees the restart eventually happens: a
    crash, an OOM kill, a `systemctl restart`, a deploy, or a run of
    reconnect failures.  Each one silently destroyed every frame the
    orchestrator had not yet collected, and the node came back up
    reporting a healthy, empty staging area with no indication that
    anything had been in it.

    That is the same failure shape as everything else this file argues
    against: not a crash, but a machine that looks well while having lost
    the work.

    `volatile` is True when we could not get a persistent directory and
    fell back to a temporary one.  It is reported rather than hidden,
    because "your frames will not survive a restart" is something an
    operator has to be able to read off `/status` rather than discover.
    """
    configured = os.environ.get("SCANNER_STAGING_DIR")
    candidates = [pathlib.Path(configured)] if configured else [DEFAULT_STAGING_ROOT]
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".writable"
            probe.write_bytes(b"")
            probe.unlink(missing_ok=True)
        except OSError:
            continue
        return root, False
    return pathlib.Path(tempfile.mkdtemp(prefix="scanner-staging-")), True


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
        #: Frames downloaded during capture, keyed by file_id, in capture
        #: order (a plain dict preserves it, and the cap evicts from the
        #: front). Needed because a body with no card keeps the frame in
        #: volatile storage tied to the session -- close the session and it
        #: is gone, so a deferred read_file() would find nothing.
        self._cache: dict[str, pathlib.Path] = {}
        #: Where each file_id sits on the camera, for the keep_on_camera
        #: case where there is no local copy to serve.
        self._camera_path: dict[str, tuple[str, str]] = {}
        self._cache_dir: pathlib.Path | None = None
        self.staging_volatile = True
        #: SHA-256 of the previous frame's bytes. The stale-buffer and
        #: same-CameraFilePath hazards both show up here as an exact
        #: repeat; see `DuplicateFrame`.
        self._last_digest: str | None = None
        self._consecutive_session_losses = 0
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
        self._adopt_body_settings()
        if self._cache_dir is None:
            root, volatile = _resolve_staging_root()
            self._cache_dir = root / self.camera_id
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self.staging_volatile = volatile
            self._adopt_orphans()

    def read_settings(self) -> dict[str, str]:
        """
        What the body says it is set to, asked now, in the node's notation.

        Distinct from `self.settings`, which is what the node believes.  The
        two disagreeing is the interesting case, and until 2026-08-12 there
        was no way to see it: `/config` was write-only and `/status` reported
        belief.
        """
        read: dict[str, str] = {}
        for name in self.SETTING_KEYS:
            try:
                raw = self._get_config(name)
            except CameraError:
                continue                      # absent or unreadable: leave it
            if raw in (None, ""):
                continue
            try:
                read[name] = canonical_setting(name, raw)
            except (ValueError, TypeError):
                continue                      # unparseable: better silent than wrong
        return read

    def _adopt_body_settings(self) -> None:
        """
        Take the body's actual exposure as the node's own, at connect.

        Until this ran, `settings` held constructor defaults the camera had
        never been told about, so `/status` described a fictional exposure
        from the moment it connected -- and said nothing to suggest the
        numbers were aspirations rather than readings.

        Measured on scanner-node-0, 2026-08-12: `/status` reported
        ISO 200, 1/125, f/8.0 while the very next frame's EXIF read
        ISO 100, 1/250, f/8.0.  Two of the three were wrong, and an
        operator dialling settings from `/status` would have been working
        from fiction.
        """
        read = self.read_settings()
        if not read:
            return
        self.settings = self.settings.merged(
            iso=int(read["iso"]) if "iso" in read else None,
            shutter=read.get("shutterspeed"),
            aperture=read.get("f-number"),
        )

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
        self.session_reopens += 1

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
            if got and not same_setting(name, value, got):
                rejected.append(f"{name}: asked {value!r}, body reports {got!r}")
            else:
                # Record the body's own read-back where there is one, in the
                # node's notation.  Recording the *asked* value would make
                # /status agree with the request rather than with the camera,
                # which is the whole failure this read-back exists to catch.
                applied[name] = canonical_setting(name, got or value)

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

    # -- staged frames -----------------------------------------------------

    @staticmethod
    def _stage_name(seq: int, frame_index: int, file_id: str) -> str:
        """
        On-disk name for a staged frame, from which the file_id is
        recoverable without a sidecar to keep in sync.

        The seq/frame prefix makes the directory sort into capture order,
        which is what the LRU cap needs after a restart -- the mtimes are
        all within a second of each other and are not a reliable order.
        """
        return f"{seq:06d}-{frame_index}-{urllib.parse.quote(file_id, safe='')}"

    @staticmethod
    def _file_id_from_stage_name(name: str) -> str | None:
        parts = name.split("-", 2)
        if len(parts) != 3:
            return None
        try:
            int(parts[0]), int(parts[1])
        except ValueError:
            return None
        return urllib.parse.unquote(parts[2])

    def _adopt_orphans(self) -> None:
        """
        Take ownership of frames left by a previous run of this process.

        The point of a persistent staging directory is that a restart does
        not destroy uncollected frames.  That only holds if the new process
        can still *find* them -- otherwise they are merely leaked rather
        than lost, which is worse: the disk fills and nothing accounts for
        it.
        """
        if self._cache_dir is None:
            return
        try:
            names = sorted(p.name for p in self._cache_dir.iterdir() if p.is_file())
        except OSError:
            return
        for name in names:
            fid = self._file_id_from_stage_name(name)
            if fid is None or fid in self._cache:
                continue
            self._cache[fid] = self._cache_dir / name
            self.frames_recovered += 1

    def staged_frames(self) -> list[str]:
        return list(self._cache)

    def release_file(self, file_id: str) -> bool:
        existed = file_id in self._cache
        path = self._cache.pop(file_id, None)
        self._camera_path.pop(file_id, None)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return existed

    def release_cache(self) -> None:
        """
        Drop every staged frame, keeping the staging area usable.

        It previously did `shutil.rmtree(self._cache_dir)` and set
        `_cache_dir = None`, which made it a one-shot: after it ran,
        `staging_free_bytes()` reported -1 so the preflight went silent,
        and `_shoot` skipped caching entirely because `_cache_dir is None`
        -- so the next capture downloaded a frame, stored it nowhere, and
        `read_file` then went back to a camera whose session no longer held
        it.  Calling the one method that frees space was the way to start
        losing frames.  It never bit because nothing called it.
        """
        for fid in list(self._cache):
            self.release_file(fid)
        self._last_digest = None

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

        camera_path = f"{path.folder.rstrip('/')}/{path.name}"
        # The camera-side name is NOT a usable identity.  With
        # session_per_capture the body starts each session at
        # `capt_DSC00001.ARW`, so every frame of a run arrives with the
        # same name -- and keying the cache on it meant each new frame
        # evicted the previous one from the map while leaving its bytes on
        # disk, unreferenced and unreleasable.  `/files/<id>` then always
        # served the newest frame whatever was asked for.  The id is the
        # node's to mint, and the sequence is the thing that is unique.
        fid = f"{self.camera_id}/{seq:06d}/{frame_index}/{path.name}"
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

            # Before anything is written or counted.  A duplicate is not a
            # frame we happen to already have -- it is evidence that this
            # capture did not produce a new image, and everything after
            # this point would launder it into one.
            self._check_not_duplicate(data)
            self._check_optical_state(data)

            if self._cache_dir is not None:
                dest = self._cache_dir / self._stage_name(seq, frame_index, fid)
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
        else:
            self._camera_path[fid] = (path.folder, path.name)

        self.frames_captured += 1
        self.enforce_frame_cap()
        return CapturedFile(
            file_id=fid,
            camera_id=self.camera_id,
            seq=seq,
            frame_index=frame_index,
            size_bytes=size,
            latency_s=round(time.perf_counter() - t0, 3),
            path=camera_path,
        )

    def _check_not_duplicate(self, data: bytes) -> None:
        digest = hashlib.sha256(data).hexdigest()
        if self._last_digest is not None and digest == self._last_digest:
            # Do not keep the digest of a frame we are rejecting: the next
            # capture must be compared against the last frame the node
            # actually accepted, or one stale frame poisons every retry.
            raise DuplicateFrame(
                f"{self.camera_id}: this frame is byte-identical to the previous "
                f"one (sha256 {digest[:12]}).  On a real sensor that cannot "
                f"happen -- noise alone makes every frame unique -- so the body "
                f"almost certainly served a frame left in its volatile buffer by "
                f"an earlier failed capture.  A power cycle does NOT clear it; "
                f"only a USB re-enumeration does.  The frame was not staged and "
                f"frames_captured was not advanced."
            )
        self._last_digest = digest

    def _check_optical_state(self, data: bytes) -> None:
        """
        Notice that the lens moved, from the frame we already have.

        Costs one EXIF tag on a file that was being parsed anyway, and
        catches the single most expensive silent failure available to this
        rig: a power cycle resetting the E PZ 16-50 to 16 mm, after which
        every frame is well exposed, sharp, correctly named -- and at the
        wrong magnification, with the DPI and distortion calibration void.
        """
        from ...calib.exposure import read_exif_exposure

        try:
            focal = read_exif_exposure(data).get("focal_length_mm", 0.0)
        except Exception:  # noqa: BLE001
            return
        if not focal:
            return
        self.focal_length_mm = focal
        want = self.expected_focal_length_mm
        if not want:
            return
        # A tenth of a millimetre: far tighter than any real zoom step,
        # loose enough for the rational-to-float rounding in EXIF.
        if abs(focal - want) > 0.1:
            self.optical_alarm = (
                f"{self.camera_id}: focal length is {focal:g} mm, calibrated at "
                f"{want:g} mm.  The lens has moved since calibration -- on the "
                f"E PZ 16-50 this is what a power cycle does, and it resets to "
                f"16 mm.  Magnification, DPI and the distortion profile are all "
                f"void until the zoom is restored and the rig re-calibrated."
            )
            raise OpticalStateChanged(self.optical_alarm)
        self.optical_alarm = ""

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
        #
        # Reading does NOT release.  A GET that consumes would make a
        # retried request after a network timeout destroy a page silently,
        # and would put the only copy of a frame at the mercy of the
        # flakiest part of the system.  Releasing is a thing the
        # orchestrator asks for -- DELETE /files/<id> -- never a side
        # effect of looking.
        cached = self._cache.get(file_id)
        if cached is not None and cached.exists():
            return cached.read_bytes()

        cam, gp = self._require()
        loc = self._camera_path.get(file_id)
        if loc is None:
            # Not staged and not on the camera: this id was released, or
            # evicted by the cap, or belongs to a run whose staging was
            # lost.  KeyError so the API answers 404 rather than blaming
            # the camera for a 503.
            raise KeyError(file_id)
        folder, name = loc
        data = self._download(folder, name)
        if not self.keep_on_camera:
            try:
                cam.file_delete(folder or "/", name)
            except Exception:  # noqa: BLE001
                pass
        return data

    # -- deep recovery -----------------------------------------------------

    def usb_reset(self) -> bool:
        """
        Re-enumerate the camera on the USB bus.  True if a device was reset.

        The recovery tier below `reconnect()`, and the only one that clears
        a frame stuck in the body's volatile buffer.  libgphoto2's own
        comment is the authority: *"Camera on-off does not delete RAM.
        Just USB reconnection helps."*  So when a `DuplicateFrame` fires,
        telling the operator to power-cycle the body is advice that cannot
        work; this is what does.

        Uses `USBDEVFS_RESET` on the device node, which needs write access
        to `/dev/bus/usb/BBB/DDD`.  The provisioner's udev rule already
        grants that to `plugdev`, which the unit joins -- so this works
        unprivileged on a provisioned node and quietly returns False
        anywhere else rather than pretending it succeeded.
        """
        import fcntl

        USBDEVFS_RESET = ord("U") << 8 | 20  # _IO('U', 20)
        reset_any = False
        try:
            devices = sorted(pathlib.Path("/sys/bus/usb/devices").iterdir())
        except OSError:
            return False
        for dev in devices:
            try:
                if (dev / "idVendor").read_text().strip().lower() != "054c":
                    continue
                busnum = int((dev / "busnum").read_text())
                devnum = int((dev / "devnum").read_text())
            except (OSError, ValueError):
                continue
            node = pathlib.Path(f"/dev/bus/usb/{busnum:03d}/{devnum:03d}")
            try:
                fd = os.open(node, os.O_WRONLY)
            except OSError:
                continue
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                reset_any = True
            except OSError:
                pass
            finally:
                os.close(fd)
        if reset_any:
            self.usb_resets += 1
            # The handle refers to a device that has just gone away and
            # come back with a new address.  Anything still holding it will
            # fail in a way that reads like a different fault.
            try:
                self.disconnect()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(2.0)
        return reset_any

    def recover(self) -> None:
        """
        Escalate: reopen the session, then re-enumerate the bus.

        `reconnect()` alone is the right first move and is usually enough.
        It is not enough for the one failure it cannot see -- a body whose
        buffer is holding a stale frame will reconnect perfectly and go on
        serving the wrong image.
        """
        try:
            self.reconnect()
            return
        except CameraError:
            pass
        if self.usb_reset():
            self.reconnect()
            return
        raise CameraDisconnected(
            f"{self.camera_id}: reconnect failed and a USB re-enumeration did "
            f"not help.  Pull the cable and put it back -- do not power-cycle "
            f"the body; that does not clear its buffer and it resets a taped "
            f"zoom to 16 mm."
        )

    def preview(self) -> bytes:
        cam, gp = self._require()
        try:
            cam_file = cam.capture_preview()
            return memoryview(cam_file.get_data_and_size()).tobytes()
        except gp.GPhoto2Error as e:
            raise CameraError(f"{self.camera_id}: preview failed: {e}") from e
