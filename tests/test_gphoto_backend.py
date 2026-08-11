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

from scanner.node.backends.base import CameraSettings, ConfigUnsupported


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


class FakePath:
    folder = "/"
    name = "capt_DSC00001.ARW"


class FakeCamera:
    def __init__(self):
        self.cfg = None
        self.deleted: list[str] = []
        self.on_camera: list[str] = []

    def init(self):
        pass

    def exit(self):
        pass

    def get_config(self):
        return self.cfg

    def set_config(self, cfg):
        self.cfg = cfg

    def capture(self, _kind):
        self.on_camera.append(FakePath.name)
        return FakePath()

    def file_get_info(self, _folder, _name):
        return types.SimpleNamespace(file=types.SimpleNamespace(size=24_000_000))

    def file_get(self, _folder, _name, _type):
        return FakeCameraFile()

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
    assert cam.on_camera, "frame should exist on the body before download"
    dev.read_file(files[0].file_id)
    assert cam.deleted == ["capt_DSC00001.ARW"]
    assert cam.on_camera == [], "frame must not be left on a body with no card"


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
# never claim a setting that did not take
# --------------------------------------------------------------------------

def test_verified_settings_are_recorded(a6000):
    dev, _ = a6000
    dev.apply_settings(CameraSettings(iso=100, shutter="1/125", aperture="f/8"))
    assert dev.settings.iso == 100
    assert dev.settings.aperture == "f/8"
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
