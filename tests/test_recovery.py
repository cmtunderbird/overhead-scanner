"""
Recovery: what happens when the link stops answering.

Three tiers, and the value of each is entirely in whether anything calls
it:

  1. reopen the PTP session -- for a session that has merely gone idle;
  2. re-enumerate the USB device -- for a link that has *wedged*, where a
     reopen cannot help because `cam.init()` is itself what times out;
  3. give up and say so, in words that point at the cable rather than at
     the body's power switch.

Measured on `scanner-node-0`, 2026-08-12, after ~7 hours idle:

  * every path returned `[-10] Timeout reading from or writing to the
    port` -- init, capture, preview, and the `gphoto2` CLI alike;
  * a full `systemctl restart` left the fault exactly where it was, so
    the wedge was device-level and not ours;
  * `usb_reset()` cleared it, unprivileged, and `gphoto2 --summary` then
    worked twice in a row.

And the defect that made all of that unreachable: `capture_with_retry`
called `reconnect()` directly, so `recover()` -- and the `usb_reset()`
inside it -- had no caller. A recovery tier nothing invokes looks like
protection and is not. That is the same shape as `release_cache()` sitting
unreferenced, shipped in the change that fixed it.
"""
from __future__ import annotations

import pytest

from scanner.node.backends.base import CameraDisconnected, CameraError
from tests.test_gphoto_backend import FakeError, install_fake


@pytest.fixture
def dev(monkeypatch, tmp_path):
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)
    cam.USB_SETTLE_S = 0.0
    cam.connect()
    return cam, fake


# ------------------------------------------------------ classifying -10 --

def test_timeout_is_a_session_lost_code(dev):
    """
    -10 was missing, and -10 is the one that actually happens.  Without it
    a wedged link raised a plain CameraError, which no retry path catches.
    """
    cam, _ = dev
    assert -10 in cam._session_lost_codes()


def test_a_wedged_open_is_a_disconnect_not_a_generic_error(dev, monkeypatch):
    """
    `_reopen()` failing must be catchable by `capture_with_retry`, or the
    recovery tier that fixes it never runs.
    """
    cam, fake = dev

    def wedged():
        raise FakeError("Timeout reading from or writing to the port", code=-10)

    monkeypatch.setattr(fake, "init", wedged)
    with pytest.raises(CameraDisconnected) as exc:
        cam._open_session()
    assert "wedged" in str(exc.value)
    # It must also say the thing that is true and counter-intuitive.
    assert "power-cycling" in str(exc.value)


def test_an_unrelated_error_is_still_a_plain_camera_error(dev, monkeypatch):
    """Don't launder every fault into "session lost"; -2 is not that."""
    cam, fake = dev

    def broken():
        raise FakeError("something else entirely", code=-2)

    monkeypatch.setattr(fake, "init", broken)
    with pytest.raises(CameraError) as exc:
        cam._open_session()
    assert not isinstance(exc.value, CameraDisconnected)


# ------------------------------------------------- the seam that was cut --

def test_capture_with_retry_goes_through_recover(dev, monkeypatch):
    """
    The bug, pinned.  `capture_with_retry` used to call `reconnect()`, so a
    backend's deeper tier was unreachable no matter how well it worked.
    """
    cam, _ = dev
    called = []
    monkeypatch.setattr(cam, "recover", lambda: called.append("recover"))

    attempts = {"n": 0}

    def fails_once(seq, frames=1):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise CameraDisconnected("cam0: session lost during capture")
        return ["frame"]

    monkeypatch.setattr(cam, "capture", fails_once)
    assert cam.capture_with_retry(1) == ["frame"]
    assert called == ["recover"]


def test_preview_recovers_the_way_capture_always_did(dev, monkeypatch):
    """
    The asymmetry that stranded `/preview`.  An operator framing a page is
    the most likely person to meet an idle session, because framing is
    what you do after leaving the rig alone.
    """
    cam, _ = dev
    called = []
    monkeypatch.setattr(cam, "recover", lambda: called.append("recover"))

    attempts = {"n": 0}

    def fails_once():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise CameraDisconnected("cam0: session lost during preview")
        return b"\xff\xd8jpeg"

    monkeypatch.setattr(cam, "preview", fails_once)
    assert cam.preview_with_retry() == b"\xff\xd8jpeg"
    assert called == ["recover"]


def test_preview_reraises_when_recovery_does_not_help(dev, monkeypatch):
    cam, _ = dev
    monkeypatch.setattr(cam, "recover", lambda: None)
    monkeypatch.setattr(
        cam, "preview",
        lambda: (_ for _ in ()).throw(CameraDisconnected("cam0: still gone")),
    )
    with pytest.raises(CameraDisconnected):
        cam.preview_with_retry()


def test_a_session_lost_preview_is_classified(dev, monkeypatch):
    """`[-7] I/O problem` is what /preview actually returned on 12 Aug."""
    cam, fake = dev

    def io_problem():
        raise FakeError("I/O problem", code=-7)

    monkeypatch.setattr(fake, "capture_preview", io_problem)
    with pytest.raises(CameraDisconnected):
        cam.preview()


# ------------------------------------------------------ the escalation --

def test_recover_escalates_to_a_bus_reset_when_reconnect_fails(dev, monkeypatch):
    cam, _ = dev
    order = []

    def reconnect(attempts=3, backoff_s=0.5):
        order.append(f"reconnect({attempts})")
        if len(order) == 1:
            raise CameraDisconnected("cam0: still wedged")

    monkeypatch.setattr(cam, "reconnect", reconnect)
    monkeypatch.setattr(cam, "usb_reset", lambda: order.append("usb_reset") or True)

    cam.recover()
    assert order[0].startswith("reconnect")
    assert order[1] == "usb_reset"
    # ...and the reconnect after a re-enumeration is the patient one: the
    # bus has just dropped and re-added the device, so the first attempts
    # are expected to fail and must not exhaust the budget.
    assert order[2] == "reconnect(5)"


def test_recover_does_not_reset_the_bus_when_a_reconnect_is_enough(dev, monkeypatch):
    """A bus re-enumeration is disruptive; don't reach for it first."""
    cam, _ = dev
    reset = []
    monkeypatch.setattr(cam, "reconnect", lambda *a, **k: None)
    monkeypatch.setattr(cam, "usb_reset", lambda: reset.append(1) or True)
    cam.recover()
    assert reset == []


def test_giving_up_names_the_cable_and_the_card(dev, monkeypatch):
    """
    The message is the whole value of the failure.  Telling an operator to
    power-cycle the body is advice that cannot work -- it does not clear
    the buffer, and it resets a taped zoom to 16 mm.
    """
    cam, _ = dev
    monkeypatch.setattr(
        cam, "reconnect",
        lambda *a, **k: (_ for _ in ()).throw(CameraDisconnected("no")),
    )
    monkeypatch.setattr(cam, "usb_reset", lambda: False)
    # The body is still enumerated -- it is wedged, not absent.  Pinned
    # explicitly rather than left to whatever is plugged into the machine
    # running the tests.
    monkeypatch.setattr(cam, "sony_on_bus", lambda: True)

    with pytest.raises(CameraDisconnected) as exc:
        cam.recover()
    msg = str(exc.value)
    assert "Pull the cable" in msg
    assert "do not power-cycle" in msg
    assert "card is seated" in msg      # reads succeed, shutter times out


def test_giving_up_on_an_absent_body_does_not_forbid_a_power_cycle(dev, monkeypatch):
    """
    The opposite case, and the dangerous one.  If the body has left the bus
    -- a flat battery, 2026-08-12 -- then "do not power-cycle the body" is
    advice that forbids the only thing that can work.  It must not be said
    when there is nothing there to power-cycle.
    """
    cam, _ = dev
    monkeypatch.setattr(
        cam, "reconnect",
        lambda *a, **k: (_ for _ in ()).throw(CameraDisconnected("no")),
    )
    monkeypatch.setattr(cam, "usb_reset", lambda: False)
    monkeypatch.setattr(cam, "sony_on_bus", lambda: False)

    with pytest.raises(CameraDisconnected) as exc:
        cam.recover()
    msg = str(exc.value)
    assert "do not power-cycle" not in msg
    assert "battery" in msg
    assert "16 mm" in msg               # warn before they wonder why scale moved


def test_an_unreadable_bus_falls_back_to_the_conservative_message(dev, monkeypatch):
    """
    `sony_on_bus()` returns None when sysfs cannot be read.  Unknown must
    not be treated as absent: telling someone to swap a healthy battery is
    a smaller error than telling them to power-cycle a wedged body, but it
    is still an error, and the wedged case is the one we can still fix from
    software.
    """
    cam, _ = dev
    monkeypatch.setattr(
        cam, "reconnect",
        lambda *a, **k: (_ for _ in ()).throw(CameraDisconnected("no")),
    )
    monkeypatch.setattr(cam, "usb_reset", lambda: False)
    monkeypatch.setattr(cam, "sony_on_bus", lambda: None)

    with pytest.raises(CameraDisconnected) as exc:
        cam.recover()
    assert "Pull the cable" in str(exc.value)


def test_the_base_recover_is_just_a_reconnect(monkeypatch):
    """
    Backends without a deeper tier must still route through the seam, or
    the next tier added will be unreachable all over again.
    """
    from scanner.node.backends.mock import MockCamera

    cam = MockCamera("cam0", 0, scale=0.05)
    cam.connect()
    called = []
    monkeypatch.setattr(cam, "reconnect", lambda *a, **k: called.append(1))
    cam.recover()
    assert called == [1]


# ------------------------------------------------- prerequisites at connect --
#
# Measured on scanner-node-0, 2026-08-12:
#
#     focusmode      Current: DMF
#     expprogram     Current: M
#     capturemode    Current: Single Shot
#
# DMF made the body refuse the shutter.  Every capture returned
# `[-1] Unspecified error`, the session died behind it, and the gphoto2 CLI
# failed identically -- so the node was exonerated and the evening went on a
# link fault that was really an AF interlock.  One PTP write of
# focusmode=Manual fixed it and the next capture produced a 24,513,792-byte
# ARW.  The backlog had this filed as cosmetic.

def test_focus_mode_is_set_not_inherited(dev):
    cam, fake = dev
    assert str(fake.cfg.get_child_by_name("focusmode").get_value()) == "Manual"


def test_drive_mode_is_set_not_inherited(dev):
    """
    Independently paid for: a drive mode left in Continuous Low Speed
    overnight produced an hour of confusing double-exposures.
    """
    cam, fake = dev
    assert str(fake.cfg.get_child_by_name("capturemode").get_value()) == "Single Shot"


def test_a_body_left_in_dmf_is_corrected_at_connect(monkeypatch, tmp_path):
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    fake.cfg.get_child_by_name("focusmode").set_value("DMF")
    assert str(fake.cfg.get_child_by_name("focusmode").get_value()) == "DMF"

    cam = GPhotoCamera("cam0", 0)
    cam.connect()
    assert str(fake.cfg.get_child_by_name("focusmode").get_value()) == "Manual"
    assert "focusmode" not in cam.last_error


def test_a_body_that_ignores_the_write_is_reported(monkeypatch, tmp_path):
    """
    Accepting a write and quietly ignoring it is the failure this whole
    file exists to catch.  Say so rather than assuming it took.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    node = fake.cfg.get_child_by_name("focusmode")
    node.set_value("DMF")
    node.sticky = "DMF"          # writes land, value never changes

    cam = GPhotoCamera("cam0", 0)
    cam.connect()
    assert "focusmode" in cam.last_error
    assert "focus lock" in cam.last_error


def test_expprogram_is_left_alone(dev):
    """
    Read-only over PTP.  The mode dial is genuinely the operator's job and
    pretending otherwise would put a lie in last_error.
    """
    cam, _ = dev
    assert all(name != "expprogram" for name, _ in cam.REQUIRED_MODES)


# ------------------------------------------- naming the right cause first --
#
# Measured on scanner-node-0, 2026-08-12.  The body went from serving
# 24,513,792-byte ARWs to this, between two commands:
#
#     cam0: reconnect failed after 5 attempts: cam0: no camera found.
#     Check the cable carries data ...
#     usb_resets: 1
#
# The recovery tier behaved perfectly -- retried, escalated to a bus reset,
# gave up in words.  The words were the problem.  The battery had died, and
# the message sent the operator to the one component that was demonstrably
# fine: a cable that had been carrying 24 MB frames all evening cannot be
# a charge-only lead.
#
# GP_ERROR_MODEL_NOT_FOUND cannot distinguish these causes -- the device is
# simply not enumerated -- so the message cannot know.  What it *can* know
# is whether this body has ever answered, and that is enough to order the
# causes by likelihood instead of by the order someone happened to write
# them in.

def _model_not_found(fake, monkeypatch):
    def gone():
        raise FakeError("Could not find the requested device", code=-105)
    monkeypatch.setattr(fake, "init", gone)


def test_a_body_that_never_answered_blames_the_cable_first(monkeypatch, tmp_path):
    """
    Nothing has worked yet, so this is a setup fault.  A charge-only lead
    enumerates nothing at all, which is this exact error.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)
    _model_not_found(fake, monkeypatch)

    with pytest.raises(CameraDisconnected) as exc:
        cam._open_session()
    msg = str(exc.value)
    assert msg.index("cable") < msg.index("battery")


def test_a_body_that_answered_then_vanished_blames_the_battery_first(
    monkeypatch, tmp_path
):
    """
    The regression this file was extended for.  A cable that has carried
    frames is not the suspect; the A6000 leaves the bus without warning
    when the cell goes flat, and that is what this looks like.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)
    cam.connect()                      # it worked once -- that is the whole signal
    _model_not_found(fake, monkeypatch)

    with pytest.raises(CameraDisconnected) as exc:
        cam._open_session()
    msg = str(exc.value)
    assert msg.index("battery") < msg.index("cable")
    # ...and say why the ordering moved, so it does not read as a guess.
    assert "answering earlier" in msg


def test_neither_cause_is_ever_dropped(monkeypatch, tmp_path):
    """
    Reorder, never truncate.  The less likely cause is still a cause, and
    an operator who has already checked the battery needs the next thing to
    try in the same message rather than in a second failure ten minutes on.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)

    for ever_opened in (False, True):
        cam._ever_opened = ever_opened
        _model_not_found(fake, monkeypatch)
        with pytest.raises(CameraDisconnected) as exc:
            cam._open_session()
        msg = str(exc.value)
        assert "battery" in msg
        assert "charge-only" in msg
        assert "PC Remote" in msg


def test_ever_opened_survives_a_disconnect(monkeypatch, tmp_path):
    """
    `disconnect()` is the normal end of a session, not evidence that the
    cable was never good.  If it cleared the flag, the very next failure
    after an ordinary close would blame the cable again.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)
    cam.connect()
    cam.disconnect()
    assert cam._ever_opened is True


def test_an_absent_mode_is_recorded_not_fatal(monkeypatch, tmp_path):
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)
    cam = GPhotoCamera("cam0", 0)
    monkeypatch.setattr(cam, "has_config", lambda n: n not in ("focusmode",))
    cam.connect()
    assert "focusmode" in cam.unsupported_settings
