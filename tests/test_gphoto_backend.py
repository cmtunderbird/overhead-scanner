"""
The gphoto2 backend, against a simulated body.

There is no camera in CI, so these drive a fake `gphoto2` module shaped
like the real one.  The fake exists to encode a measured fact: the
ILCE-6000 has **no `capturetarget` key at all** (see
`first-light-measurements.md`), and code that assumes otherwise degrades
in two silent ways --

  * it reports settings the body never accepted, and
  * it leaves every frame in the body's volatile storage, because
    "keep on the card" is meaningless when nothing reaches a card.

Both are the same failure shape as the mock-backend trap: something that
looks healthy while doing nothing.
"""
from __future__ import annotations

import sys
import types

import pytest

from scanner.node.backends.base import (
    CameraDisconnected, CameraSettings, ConfigUnsupported,
)


# --------------------------------------------------------------------------
# A fake libgphoto2, shaped like the real binding.
# --------------------------------------------------------------------------

class FakeError(Exception):
    def __init__(self, msg="fake", code=-1):
        super().__init__(msg)
        self.code = code


class Widget:
    """A config node. Leaves hold values; branches hold children."""

    def __init__(self, name, value=None, children=None, readonly=False):
        self._name = name
        self._value = value
        self._children = children or []
        self.readonly = readonly
        #: set to a fixed value to simulate a body that accepts a write
        #: and then quietly ignores it
        self.sticky = None

    def get_name(self):
        return self._name

    def count_children(self):
        return len(self._children)

    def get_child(self, i):
        return self._children[i]

    def get_child_by_name(self, name):
        if self._name == name:
            return self
        for c in self._children:
            try:
                return c.get_child_by_name(name)
            except FakeError:
                continue
        raise FakeError(f"no widget named {name}", code=-2)

    def get_value(self):
        return self._value

    def set_value(self, v):
        if self.readonly:
            raise FakeError(f"{self._name} is read-only", code=-2)
        self._value = self.sticky if self.sticky is not None else v


def _tree(*, capturetarget: bool):
    """A config tree. `capturetarget=False` models the real ILCE-6000."""
    imgsettings = Widget("imgsettings", children=[Widget("iso", "Auto ISO")])
    capturesettings = Widget("capturesettings", children=[
        Widget("shutterspeed", "1/250"),
        Widget("f-number", "f/5.6"),
        Widget("expprogram", "M", readonly=True),
        # Both default to what the real body was found in on 2026-08-12.
        # DMF is not a neutral starting point -- it is the state that made
        # the ILCE-6000 refuse the shutter for an entire evening -- so the
        # fake starts there deliberately, and a connect that does not
        # correct it fails the tests.
        Widget("focusmode", "DMF"),
        Widget("capturemode", "Continuous Low Speed"),
    ])
    status = Widget("status", children=[Widget("cameramodel", "ILCE-6000")])
    kids = [imgsettings, capturesettings, status]
    if capturetarget:
        kids.append(Widget("settings",
                           children=[Widget("capturetarget", "Internal RAM")]))
    return Widget("main", children=kids)


class FakeCameraFile:
    def __init__(self, data=b"x" * 32):
        self._d = data

    def get_data_and_size(self):
        return memoryview(self._d)


#: Every real frame differs from every other -- sensor noise alone sees to
#: that, which is exactly why a byte-identical repeat is evidence of the
#: stale-buffer hazard rather than a coincidence.  A fake that returns a
#: constant would make the duplicate guard fire on every second capture in
#: CI, so the fake models the noise.  `stuck` turns it off to reproduce the
#: hazard on purpose.


class FakePath:
    folder = "/"
    name = "capt_DSC00001.ARW"


class FakeCamera:
    def __init__(self):
        self.cfg = None
        self.deleted: list[str] = []
        self.on_camera: list[str] = []
        self.inits = 0
        self.session_shots = 0
        self.downloads = 0
        #: When True every download returns identical bytes, which is what
        #: a body with a frame stuck in RAM does.
        self.stuck = False
        #: Reproduce the measured ILCE-6000 behaviour: a session manages
        #: this many captures, then every further one fails.
        self.captures_per_session = None

    def init(self):
        self.inits += 1
        self.session_shots = 0

    def exit(self):
        pass

    def get_config(self):
        return self.cfg

    def set_config(self, cfg):
        self.cfg = cfg

    def capture(self, _kind):
        if (self.captures_per_session is not None
                and self.session_shots >= self.captures_per_session):
            # This is the real error the body returns: -1, "Unspecified".
            raise FakeError("Unspecified error", code=-1)
        self.session_shots += 1
        self.on_camera.append(FakePath.name)
        return FakePath()

    def file_get_info(self, _folder, _name):
        return types.SimpleNamespace(file=types.SimpleNamespace(size=24_000_000))

    def file_get(self, _folder, _name, _type):
        if self.stuck:
            # The body serving a frame left in its volatile buffer.
            return FakeCameraFile(b"x" * 32 + b"STALE")
        self.downloads += 1
        return FakeCameraFile(b"x" * 32 + b"%04d" % self.downloads)

    def file_delete(self, _folder, name):
        self.deleted.append(name)
        if name in self.on_camera:
            self.on_camera.remove(name)

    def capture_preview(self):
        return FakeCameraFile()


def install_fake(monkeypatch, *, capturetarget: bool):
    cam = FakeCamera()
    cam.cfg = _tree(capturetarget=capturetarget)
    mod = types.ModuleType("gphoto2")
    mod.Camera = lambda: cam
    mod.GPhoto2Error = FakeError
    mod.GP_ERROR_MODEL_NOT_FOUND = -105
    mod.GP_ERROR_IO_USB_CLAIM = -53
    mod.GP_ERROR_IO = -7
    mod.GP_ERROR_IO_USB_FIND = -52
    mod.GP_ERROR = -1
    mod.GP_CAPTURE_IMAGE = 0
    mod.GP_FILE_TYPE_NORMAL = 1
    monkeypatch.setitem(sys.modules, "gphoto2", mod)
    return cam


@pytest.fixture
def a6000(monkeypatch):
    """The body we actually own: no capturetarget."""
    from scanner.node.backends.gphoto import GPhotoCamera
    cam = install_fake(monkeypatch, capturetarget=False)
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    return dev, cam


@pytest.fixture
def with_card(monkeypatch):
    """A body that does expose capturetarget."""
    from scanner.node.backends.gphoto import GPhotoCamera
    cam = install_fake(monkeypatch, capturetarget=True)
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    return dev, cam


# --------------------------------------------------------------------------
# capturetarget: absent, not merely defaulted
# --------------------------------------------------------------------------

def test_probe_finds_the_real_keys(a6000):
    dev, _ = a6000
    assert dev.has_config("iso")
    assert dev.has_config("shutterspeed")
    assert dev.has_config("f-number")
    assert not dev.has_config("capturetarget")


def test_absent_capturetarget_is_reported_not_swallowed(a6000):
    dev, _ = a6000
    assert dev.effective_capture_target == "internal"
    assert "capturetarget" in dev.unsupported_settings
    # The old code caught the error and passed, leaving no trace anywhere.
    assert "capturetarget" in dev.last_error
    assert dev.status().effective_capture_target == "internal"


def test_setting_an_absent_key_raises_the_specific_error(a6000):
    dev, _ = a6000
    with pytest.raises(ConfigUnsupported):
        dev._set_config("capturetarget", "Memory card")


def test_card_body_reports_card(with_card):
    dev, _ = with_card
    assert dev.effective_capture_target == "card"
    assert "capturetarget" not in dev.unsupported_settings


# --------------------------------------------------------------------------
# the storage leak
# --------------------------------------------------------------------------

def test_frames_are_deleted_when_the_body_has_no_card(a6000):
    """
    The bug this guards: `keep_on_camera` defaulted True, so on a body
    with no card target every frame stayed in volatile storage until
    capture failed.
    """
    dev, cam = a6000
    assert dev.keep_on_camera is False
    files = dev.capture(seq=1)
    # Pulled during capture, not deferred: the frame lives only as long as
    # the session that made it, and that session is about to close.
    assert cam.deleted == ["capt_DSC00001.ARW"]
    assert cam.on_camera == [], "frame must not be left on a body with no card"
    # And it is still readable afterwards, from the local cache.
    assert dev.read_file(files[0].file_id).startswith(b"x" * 32)


def test_frames_are_kept_when_the_body_does_have_a_card(with_card):
    dev, cam = with_card
    assert dev.keep_on_camera is True
    files = dev.capture(seq=1)
    dev.read_file(files[0].file_id)
    assert cam.deleted == []


def test_explicit_override_is_honoured(monkeypatch):
    from scanner.node.backends.gphoto import GPhotoCamera
    install_fake(monkeypatch, capturetarget=False)
    dev = GPhotoCamera("cam0", 0, keep_on_camera=True)
    dev.connect()
    assert dev.keep_on_camera is True


# --------------------------------------------------------------------------
# one capture per PTP session
# --------------------------------------------------------------------------

def test_a_fresh_session_is_opened_for_every_frame(a6000):
    dev, cam = a6000
    before = cam.inits
    dev.capture(seq=1, frames=3)
    assert cam.inits - before == 3, "each frame must get its own PTP session"


def test_three_frames_succeed_against_a_one_capture_per_session_body(a6000):
    """
    The measured failure, reproduced: 0/3 on a persistent session,
    3/3 with a fresh session per frame. Nothing else differs.
    """
    dev, cam = a6000
    cam.captures_per_session = 1

    files = dev.capture(seq=1, frames=3)
    assert len(files) == 3
    assert dev.frames_captured == 3


def test_persistent_session_still_fails_on_such_a_body(a6000):
    """The control. If this ever passes, the test above proves nothing."""
    dev, cam = a6000
    cam.captures_per_session = 1
    dev.session_per_capture = False

    with pytest.raises(CameraDisconnected):
        dev.capture(seq=1, frames=3)


def test_unspecified_error_counts_as_a_lost_session(a6000):
    """
    GP_ERROR (-1) is the observed death. Before it was listed, capture()
    raised a plain CameraError and `capture_with_retry` -- the base
    class's only recovery path -- never fired for the one failure that
    actually happens.
    """
    dev, cam = a6000
    cam.captures_per_session = 0
    dev.session_per_capture = False

    with pytest.raises(CameraDisconnected):
        dev.capture(seq=1)


def test_capture_with_retry_now_recovers(a6000):
    dev, cam = a6000
    cam.captures_per_session = 1
    dev.session_per_capture = False   # force the failure...
    calls = {"n": 0}
    real_reconnect = dev.reconnect

    def reconnect():
        calls["n"] += 1
        dev.session_per_capture = True   # ...and let recovery fix it
        real_reconnect()

    dev.reconnect = reconnect
    dev.capture(seq=1)                   # primes the session
    files = dev.capture_with_retry(seq=2, frames=1)
    assert files and calls["n"] >= 1, "the base-class retry must engage"


# --------------------------------------------------------------------------
# never claim a setting that did not take
# --------------------------------------------------------------------------

def test_verified_settings_are_recorded(a6000):
    dev, _ = a6000
    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="f/8"))
    assert dev.settings.iso == 100
    # Recorded in the node's own notation -- the bare number -- rather than
    # in whichever spelling the caller happened to use.  Before 2026-08-12
    # this stored the asked string verbatim, so the same aperture could be
    # recorded as 'f/8', 'f8' or '8.0' depending on who set it, and a
    # read-back comparison against the body's spelling could never succeed.
    assert dev.settings.aperture == "8"
    assert dev.last_error == ""


def test_a_silently_ignored_write_is_not_reported_as_applied(a6000):
    """
    A body that accepts the write and keeps its old value used to be
    indistinguishable from success: `self.settings = settings` ran
    regardless.  Read-back is what makes the difference visible.
    """
    dev, cam = a6000
    cam.cfg.get_child_by_name("f-number").sticky = "f/5.6"

    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="f/8"))

    assert dev.settings.aperture != "f/8", "must not claim a value the body refused"
    assert "f-number" in dev.last_error
    assert dev.settings.iso == 100, "the settings that did take are still recorded"


def test_readonly_key_is_reported_as_rejected_not_applied(a6000):
    dev, cam = a6000
    cam.cfg.get_child_by_name("iso").readonly = True
    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="f/8"))
    assert "iso" in dev.last_error
    assert dev.settings.iso != 100


def test_unsupported_is_distinguished_from_rejected(monkeypatch):
    from scanner.node.backends.gphoto import GPhotoCamera
    cam = install_fake(monkeypatch, capturetarget=False)
    # Remove f-number entirely: absent, not rejected.
    caps = cam.cfg.get_child_by_name("capturesettings")
    caps._children = [c for c in caps._children if c.get_name() != "f-number"]
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="f/8"))
    assert "f-number" in dev.unsupported_settings
    assert "unsupported" in dev.last_error
    assert "f-number" in dev.status().unsupported_settings


# --------------------------------------------------------------------------
# regression: a failed probe must not look like "supports nothing"
# --------------------------------------------------------------------------

def test_failed_probe_says_so(monkeypatch):
    from scanner.node.backends.gphoto import GPhotoCamera
    cam = install_fake(monkeypatch, capturetarget=False)

    def boom():
        raise FakeError("tree unavailable")

    cam.get_config = boom
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    assert "config probe failed" in dev.last_error


# --------------------------------------------------------------------------
# a frame that was not secured is not a captured frame
#
# Both of these were reproduced on scanner-node-0 (Pi 5, ILCE-6000) on
# 2026-08-12 by filling the staging tmpfs to ENOSPC.  The node had already
# fired the shutter and pulled the whole 24.5 MB off the body before the
# write failed -- so an actuation and a transfer were spent, and the caller
# was told only "Internal Server Error".
# --------------------------------------------------------------------------

def test_staging_write_failure_is_reported_as_staging_not_camera(a6000, monkeypatch):
    import pathlib
    from scanner.node.backends.base import StagingFull

    dev, _ = a6000

    def enospc(self, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pathlib.Path, "write_bytes", enospc)

    with pytest.raises(StagingFull) as e:
        dev.capture(seq=1)

    msg = str(e.value)
    # It must name the real fault. "capture failed" would send an operator
    # back to the camera, which is the one place nothing is wrong.
    assert "staging" in msg.lower()
    # And it must be honest that the expensive part already happened.
    assert "already downloaded" in msg
    # The counter must not claim a frame that does not exist.
    assert dev.frames_captured == 0


def test_download_failure_is_not_swallowed(a6000, monkeypatch):
    """
    A body with no card holds the frame only for the life of the session.

    If the download fails there is nothing left to fetch later, so
    returning a CapturedFile with a plausible size_bytes -- as this did
    until 2026-08-12 -- hands the caller a file_id for a frame that has
    already ceased to exist.
    """
    from scanner.node.backends.base import CameraError

    dev, _ = a6000

    def boom(folder, name):
        raise CameraError("cam0: download failed")

    monkeypatch.setattr(dev, "_download", boom)

    with pytest.raises(CameraError):
        dev.capture(seq=1)
    assert dev.frames_captured == 0


def test_preflight_refuses_before_the_shutter(a6000, monkeypatch):
    """
    The cheap fix for the expensive failure above: when the space is
    knowable in advance, never spend the actuation at all.
    """
    from scanner.node.backends.base import StagingFull

    dev, cam = a6000
    fired = []
    real_capture = cam.capture
    cam.capture = lambda *a, **k: (fired.append(1), real_capture(*a, **k))[1]

    monkeypatch.setattr(dev, "staging_free_bytes", lambda: 4 * 1024 * 1024)

    with pytest.raises(StagingFull) as e:
        dev.capture(seq=1)
    assert "No shutter actuation" in str(e.value)
    assert fired == [], "the shutter must not have been used"


# --------------------------------------------------------------------------
# notation is not disagreement
#
# Measured on scanner-node-0, 2026-08-12.  POST /config asked for f/5.6:
#
#   /status  "aperture":"8.0"                       <- stale, the old value
#   last_error "f-number: asked '5.6', body reports 'f/5.6'"
#   EXIF     F Number : 5.6                         <- it had worked
#
# The write succeeded, the read-back succeeded, and a string comparison
# threw the result away.
# --------------------------------------------------------------------------

def test_f_prefix_is_not_a_rejection(a6000, monkeypatch):
    from scanner.node.backends.gphoto import canonical_setting

    dev, _ = a6000
    real_get = dev._get_config

    def sony_spelling(name):
        if name == "f-number":
            return "f/5.6"          # what the body actually says
        return real_get(name)

    monkeypatch.setattr(dev, "_get_config", sony_spelling)
    dev.apply_settings(CameraSettings(iso=400, shutter="1/60", aperture="5.6"))

    assert dev.last_error == "", "a correct write must not be reported as rejected"
    assert dev.settings.aperture == canonical_setting("f-number", "5.6")
    assert dev.settings.iso == 400


def test_unreduced_shutter_fraction_is_not_a_rejection(a6000, monkeypatch):
    """
    Sony reports 1/2 s as the unreduced '5/10'.

    Text-compared that is a mismatch; as an exposure it is identical. This
    is the slow end of the range, which is exactly where a scanner in poor
    light ends up.
    """
    dev, _ = a6000
    real_get = dev._get_config

    def sony_spelling(name):
        return "5/10" if name == "shutterspeed" else real_get(name)

    monkeypatch.setattr(dev, "_get_config", sony_spelling)
    dev.apply_settings(CameraSettings(iso=100, shutter="1/2", aperture="8"))
    assert dev.last_error == ""


def test_a_genuinely_different_value_is_still_a_rejection(a6000, monkeypatch):
    """
    The comparison is loosened for notation, not for value.

    The starting aperture is pinned first, because `connect()` now adopts
    the body's own value -- without that, "unchanged" and "wrongly
    accepted" look identical and the test proves nothing.
    """
    dev, _ = a6000
    dev.settings = dev.settings.merged(aperture="8")
    real_get = dev._get_config

    def wrong(name):
        return "f/11" if name == "f-number" else real_get(name)

    monkeypatch.setattr(dev, "_get_config", wrong)
    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="5.6"))
    assert "f-number" in dev.last_error
    assert dev.settings.aperture == "8", "a rejected write must not be recorded"


def test_connect_adopts_the_bodys_actual_exposure(monkeypatch):
    """
    /status must not describe a camera nobody has configured.

    Observed: /status said ISO 200, 1/125, f/8.0 -- the constructor
    defaults -- while the next frame's EXIF read ISO 100, 1/250.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    cam = install_fake(monkeypatch, capturetarget=False)
    caps = cam.cfg.get_child_by_name("capturesettings")
    caps.get_child_by_name("f-number")._value = "f/4.0"
    caps.get_child_by_name("shutterspeed")._value = "1/250"
    cam.cfg.get_child_by_name("imgsettings").get_child_by_name("iso")._value = "800"

    dev = GPhotoCamera("cam0", 0)
    dev.connect()

    assert dev.settings.iso == 800
    assert dev.settings.shutter == "1/250"
    assert dev.settings.aperture == "4"
