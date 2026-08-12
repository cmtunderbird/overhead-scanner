"""
Frame lifecycle: claiming, capping, and the two ways a frame goes wrong
without anything failing.

Everything here defends the same property, which is the one this project
keeps having to re-learn: **a node that has lost or corrupted a frame must
not look healthy.**  Three separate mechanisms could break it silently
before this file existed --

  * staging filled at ~320 frames with no route able to free it, so a run
    stopped honestly but still stopped;
  * a service restart destroyed every uncollected frame, because staging
    lived in a `PrivateTmp` tmpfs, and the node came back reporting an
    empty, healthy staging area;
  * the body could serve a frame from its own volatile buffer, so page
    N+1 arrived holding page N's pixels and nothing raised.

and a fourth that was not about frames at all: a power cycle resetting the
E PZ 16-50 to 16 mm, after which every frame is sharp, well exposed, and
at the wrong magnification.
"""
from __future__ import annotations

import pathlib

import pytest

from scanner.node.backends.base import DuplicateFrame, OpticalStateChanged
from tests.test_gphoto_backend import install_fake


@pytest.fixture
def staged(monkeypatch, tmp_path):
    """An A6000-shaped body with a staging directory we can inspect."""
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    fake = install_fake(monkeypatch, capturetarget=False)
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    return dev, fake, tmp_path / "staging"


# ---------------------------------------------------------------- claiming --

def test_a_captured_frame_is_staged_and_countable(staged):
    dev, _, _ = staged
    files = dev.capture(1)
    assert dev.staged_frames() == [files[0].file_id]
    assert dev.status().staged_frames == 1


def test_release_forgets_one_frame_and_its_bytes(staged):
    dev, _, root = staged
    files = dev.capture(1)
    on_disk = list((root / "cam0").iterdir())
    assert len(on_disk) == 1

    assert dev.release_file(files[0].file_id) is True
    assert dev.staged_frames() == []
    assert list((root / "cam0").iterdir()) == []


def test_release_is_idempotent(staged):
    """
    An orchestrator whose release timed out will retry it.  Answering
    "no such frame" the second time invites the one reflex that loses data
    -- treating a successful release as a frame worth re-fetching.
    """
    dev, _, _ = staged
    files = dev.capture(1)
    assert dev.release_file(files[0].file_id) is True
    assert dev.release_file(files[0].file_id) is False
    assert dev.release_file("never-existed") is False


def test_reading_a_frame_does_not_release_it(staged):
    """
    Read-once on GET was the tempting design.  It makes a retried GET after
    a network timeout destroy a page silently.
    """
    dev, _, _ = staged
    files = dev.capture(1)
    dev.read_file(files[0].file_id)
    dev.read_file(files[0].file_id)
    assert dev.staged_frames() == [files[0].file_id]


def test_file_ids_are_unique_even_though_the_camera_name_is_not(staged):
    """
    With `session_per_capture` the body restarts its own numbering every
    session, so every frame of a run arrives as `capt_DSC00001.ARW`.
    Keying on that meant each new frame evicted the previous one from the
    map while leaving its bytes on disk, unreferenced and unreleasable --
    and `/files/<id>` served the newest frame whatever was asked for.
    """
    dev, fake, _ = staged
    a = dev.capture(1)[0]
    b = dev.capture(2)[0]
    assert a.file_id != b.file_id
    # ...while both really did come from the same camera-side name.
    assert a.path == b.path == "/capt_DSC00001.ARW"
    assert len(dev.staged_frames()) == 2
    assert dev.read_file(a.file_id) != dev.read_file(b.file_id)


def test_release_all_clears_everything(staged):
    dev, _, _ = staged
    for seq in range(3):
        dev.capture(seq)
    assert dev.release_all() == 3
    assert dev.staged_frames() == []


def test_release_cache_leaves_the_node_able_to_capture_again(staged):
    """
    The old `release_cache()` did `rmtree` and set `_cache_dir = None`, so
    afterwards the staging preflight went silent (-1 free) and `_shoot`
    skipped caching entirely -- the next frame was downloaded, stored
    nowhere, and `read_file` went back to a session that no longer held it.
    Calling the one method that freed space was how you started losing
    frames.  It never bit because nothing called it.
    """
    dev, _, _ = staged
    dev.capture(1)
    dev.release_cache()

    assert dev.staging_free_bytes() > 0
    files = dev.capture(2)
    assert dev.staged_frames() == [files[0].file_id]
    assert dev.read_file(files[0].file_id)


# -------------------------------------------------------------------- cap --

def test_the_cap_evicts_the_oldest_and_says_so(staged):
    dev, _, _ = staged
    dev.MAX_STAGED_FRAMES = 3
    ids = [dev.capture(seq)[0].file_id for seq in range(5)]

    assert dev.staged_frames() == ids[2:]
    assert dev.status().frames_evicted == 2


def test_an_eviction_is_never_silent(staged):
    """
    A cap that discards quietly reads exactly like a cap that was never
    reached.  `frames_evicted` is published whether or not anyone asks.
    """
    dev, _, _ = staged
    dev.MAX_STAGED_FRAMES = 1
    dev.capture(1)
    dev.capture(2)
    assert dev.status().frames_evicted == 1


def test_an_evicted_frame_is_a_404_not_a_camera_fault(staged):
    dev, _, _ = staged
    dev.MAX_STAGED_FRAMES = 1
    gone = dev.capture(1)[0].file_id
    dev.capture(2)
    with pytest.raises(KeyError):
        dev.read_file(gone)


# ------------------------------------------------------------- persistence --

def test_staged_frames_survive_the_process_that_made_them(monkeypatch, tmp_path):
    """
    The whole point of moving staging out of `/tmp`.  Under
    `PrivateTmp=yes` plus `Restart=always`, every crash, OOM kill, deploy
    or `systemctl restart` destroyed the frames the orchestrator had not
    yet collected -- and the node came back reporting a healthy, empty
    staging area.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    first = GPhotoCamera("cam0", 0)
    first.connect()
    kept = [first.capture(seq)[0].file_id for seq in range(2)]
    first.disconnect()

    # A new process, same disk.
    second = GPhotoCamera("cam0", 0)
    second.connect()

    assert sorted(second.staged_frames()) == sorted(kept)
    assert second.status().frames_recovered == 2
    assert second.read_file(kept[0])


def test_recovered_frames_are_still_releasable(monkeypatch, tmp_path):
    """
    Surviving is not enough: an adopted frame that cannot be claimed is a
    leak rather than a rescue, and the disk fills with nothing accounting
    for it.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    first = GPhotoCamera("cam0", 0)
    first.connect()
    fid = first.capture(1)[0].file_id
    first.disconnect()

    second = GPhotoCamera("cam0", 0)
    second.connect()
    assert second.release_file(fid) is True
    assert second.staged_frames() == []
    assert list((tmp_path / "staging" / "cam0").iterdir()) == []


def test_volatile_staging_is_reported_not_hidden(monkeypatch, tmp_path):
    """
    If we could not get persistent storage, the operator has to be able to
    read that off /status rather than find out after a reboot.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", "/proc/definitely/not/writable")
    install_fake(monkeypatch, capturetarget=False)
    dev = GPhotoCamera("cam0", 0)
    dev.connect()
    assert dev.status().staging_volatile is True


def test_persistent_staging_says_so(staged):
    dev, _, _ = staged
    assert dev.status().staging_volatile is False


# --------------------------------------------------------------- duplicates --

def test_a_repeated_frame_is_refused(staged):
    """
    The body serving a frame left in its own volatile buffer.  libgphoto2's
    source is explicit that this happens and that "Camera on-off does not
    delete RAM.  Just USB reconnection helps."
    """
    dev, fake, _ = staged
    dev.capture(1)
    fake.stuck = True
    dev.capture(2)                       # first stale frame: new to us
    with pytest.raises(DuplicateFrame):
        dev.capture(3)                   # same bytes again


def test_a_refused_duplicate_is_not_counted_or_staged(staged):
    """
    A duplicate must not be laundered into a real frame by the accounting.
    """
    dev, fake, _ = staged
    dev.capture(1)
    fake.stuck = True
    dev.capture(2)
    before = dev.frames_captured
    staged_before = list(dev.staged_frames())

    with pytest.raises(DuplicateFrame):
        dev.capture(3)

    assert dev.frames_captured == before
    assert dev.staged_frames() == staged_before


def test_the_rejected_frame_does_not_poison_the_next_comparison(staged):
    """
    Compare against the last frame the node *accepted*.  Keeping the
    rejected digest would make one stale frame fail every retry after it.
    """
    dev, fake, _ = staged
    dev.capture(1)
    fake.stuck = True
    dev.capture(2)
    with pytest.raises(DuplicateFrame):
        dev.capture(3)

    fake.stuck = False
    assert dev.capture(4)                # recovery works immediately


def test_distinct_frames_are_never_flagged(staged):
    dev, _, _ = staged
    for seq in range(6):
        dev.capture(seq)
    assert len(dev.staged_frames()) == 6


# ------------------------------------------------------------ optical state --

def _arw_with_focal(focal_mm):
    from tests.test_exposure import _tiff_with_exif
    return _tiff_with_exif(focal=(int(focal_mm * 10), 10))


def test_focal_length_is_recorded_from_the_frame(staged, monkeypatch):
    dev, fake, _ = staged
    monkeypatch.setattr(dev, "_download", lambda f, n: _arw_with_focal(30.0))
    dev.capture(1)
    assert dev.status().focal_length_mm == pytest.approx(30.0)


def test_a_moved_zoom_stops_the_run(staged, monkeypatch):
    """
    The E PZ 16-50 resets to 16 mm on every power-off, so a battery swap
    voids the DPI and distortion calibration while capture carries on
    working perfectly.  This is the whole failure mode.
    """
    dev, _, _ = staged
    dev.expected_focal_length_mm = 30.0
    monkeypatch.setattr(dev, "_download", lambda f, n: _arw_with_focal(16.0))

    with pytest.raises(OpticalStateChanged) as exc:
        dev.capture(1)
    assert "16 mm" in str(exc.value)
    assert dev.status().optical_alarm


def test_the_calibrated_focal_length_passes_quietly(staged, monkeypatch):
    dev, _, _ = staged
    dev.expected_focal_length_mm = 30.0
    monkeypatch.setattr(dev, "_download", lambda f, n: _arw_with_focal(30.0))
    dev.capture(1)
    assert dev.status().optical_alarm == ""


def test_no_calibration_means_no_alarm(staged, monkeypatch):
    """
    An alarm nobody has calibrated for is noise, and noise gets ignored --
    including on the day it is right.
    """
    dev, _, _ = staged
    assert dev.expected_focal_length_mm == 0.0
    monkeypatch.setattr(dev, "_download", lambda f, n: _arw_with_focal(16.0))
    dev.capture(1)
    assert dev.status().optical_alarm == ""


def test_a_frame_with_no_focal_length_is_not_an_alarm(staged, monkeypatch):
    """
    Silence from the EXIF must not read as 0 mm, which would look like the
    lens had fallen off.
    """
    from tests.test_exposure import _tiff_with_exif

    dev, _, _ = staged
    dev.expected_focal_length_mm = 30.0
    monkeypatch.setattr(dev, "_download", lambda f, n: _tiff_with_exif())
    dev.capture(1)
    assert dev.status().optical_alarm == ""


# ------------------------------------------------- adopting a waking body --
#
# Caught on scanner-node-0, 2026-08-12.  The body was still coming up when
# the node connected and answered its config tree with placeholders --
# iso=0, shutterspeed=Bulb, f-number=0.  All three were adopted as fact and
# served from /status until a reconnect was forced.  GET /config, which
# exists precisely to compare belief against the hardware, is what showed
# it: read_from_body said 200 / 1/125 / f8 while node_believes said
# 0 / Bulb / 0.  The camera was right the whole time.

from scanner.node.backends.gphoto import plausible_setting


@pytest.mark.parametrize("name,value", [
    ("iso", "0"), ("iso", "-100"), ("iso", ""), ("iso", "auto"),
    ("f-number", "0"), ("f-number", "f/0"), ("f-number", ""),
    ("shutterspeed", "Bulb"), ("shutterspeed", "bulb"),
    ("shutterspeed", "Auto"), ("shutterspeed", "0"), ("shutterspeed", ""),
])
def test_placeholders_are_not_settings(name, value):
    assert plausible_setting(name, value) is False


@pytest.mark.parametrize("name,value", [
    ("iso", "200"), ("iso", "100"), ("iso", "6400"),
    ("f-number", "8"), ("f-number", "f/5.6"), ("f-number", "1.4"),
    ("shutterspeed", "1/125"), ("shutterspeed", "5/10"),
    ("shutterspeed", "2"), ("shutterspeed", "30"),
])
def test_real_settings_pass(name, value):
    assert plausible_setting(name, value) is True


def _widget(dev, name):
    return dev._camera.cfg.get_child_by_name(name)


def test_a_waking_body_is_not_believed(monkeypatch, tmp_path):
    """
    The whole failure: placeholders adopted as an exposure, and /status
    then describing a camera that cannot exist.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    dev = GPhotoCamera("cam0", 0)
    dev.ADOPT_RETRY_S = 0.0
    monkeypatch.setattr(
        GPhotoCamera, "read_settings",
        lambda self: {"iso": "0", "shutterspeed": "Bulb", "f-number": "0"},
    )
    dev.connect()

    # The constructor defaults survive; the placeholders do not.
    assert dev.settings.iso == 200
    assert dev.settings.shutter == "1/125"
    assert dev.settings.aperture == "8.0"
    assert "had not finished waking" in dev.last_error


def test_the_retry_is_what_makes_it_self_healing(monkeypatch, tmp_path):
    """
    The cause is a race, not a fault -- the body is correct a moment
    later.  Retrying once is the difference between recovering on its own
    and needing a human to notice.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    calls = {"n": 0}

    def waking(self):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"iso": "0", "shutterspeed": "Bulb", "f-number": "0"}
        return {"iso": "200", "shutterspeed": "1/125", "f-number": "8"}

    monkeypatch.setattr(GPhotoCamera, "read_settings", waking)
    dev = GPhotoCamera("cam0", 0)
    dev.ADOPT_RETRY_S = 0.0
    dev.connect()

    assert calls["n"] == 2
    assert dev.settings.iso == 200
    assert dev.settings.shutter == "1/125"
    assert dev.settings.aperture == "8"


def test_a_partly_ready_body_contributes_what_it_has(monkeypatch, tmp_path):
    """
    Refusing the whole read because one field is a placeholder would throw
    away two good values to punish one bad one.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    monkeypatch.setattr(
        GPhotoCamera, "read_settings",
        lambda self: {"iso": "400", "shutterspeed": "Bulb", "f-number": "5.6"},
    )
    dev = GPhotoCamera("cam0", 0)
    dev.ADOPT_RETRY_S = 0.0
    dev.connect()

    assert dev.settings.iso == 400
    assert dev.settings.aperture == "5.6"
    assert dev.settings.shutter == "1/125"       # kept, not overwritten
    assert "shutterspeed" in dev.last_error


def test_the_capturetarget_note_is_not_clobbered(monkeypatch, tmp_path):
    """
    _resolve_capture_target() runs first and leaves a real note in
    last_error.  Overwriting it would trade one honest message for
    another instead of reporting both.
    """
    from scanner.node.backends.gphoto import GPhotoCamera

    monkeypatch.setenv("SCANNER_STAGING_DIR", str(tmp_path / "staging"))
    install_fake(monkeypatch, capturetarget=False)

    monkeypatch.setattr(
        GPhotoCamera, "read_settings",
        lambda self: {"iso": "0", "shutterspeed": "Bulb", "f-number": "0"},
    )
    dev = GPhotoCamera("cam0", 0)
    dev.ADOPT_RETRY_S = 0.0
    dev.connect()

    assert "capturetarget" in dev.last_error
    assert "had not finished waking" in dev.last_error
