"""
Mock camera backend.

Not a stub that returns a fixed image -- a simulator that behaves badly
in the same ways the real camera does.  It has realistic transfer
latency, it drops its PTP session occasionally, and it renders a
different page each time so downstream code cannot accidentally depend
on frames being identical.

If the orchestrator is correct against this, it is correct against a
Sony.  That is the entire value of building Phase 0 first.
"""

from __future__ import annotations

import io
import time
import uuid

import cv2
import numpy as np

from ...geometry import RigGeometry, BLUEPRINT_V1
from ...synth.camera import CameraDefects, BODY_A, BODY_B, simulate_capture
from ...synth.page import render_page
from .base import (
    CameraBackend,
    CameraDisconnected,
    CameraSettings,
    CapturedFile,
)

BODIES = [BODY_A, BODY_B]


class MockCamera(CameraBackend):
    """A Sony A6000 that exists only in software."""

    backend_name = "mock"

    def __init__(
        self,
        camera_id: str = "cam0",
        tile_index: int = 0,
        geom: RigGeometry = BLUEPRINT_V1,
        *,
        scale: float = 0.25,
        page_px_per_mm: float = 6.0,
        transfer_mb_s: float = 15.0,
        file_mb: float = 24.0,
        drop_every: int = 0,
        defects: CameraDefects | None = None,
        seed: int = 0,
    ):
        super().__init__(camera_id, tile_index)
        self.geom = geom
        self.scale = scale
        self.page_px_per_mm = page_px_per_mm
        self.transfer_mb_s = transfer_mb_s
        self.file_mb = file_mb
        #: Drop the PTP session every N captures.  0 disables.  Set it to
        #: something small when testing the orchestrator's retry path.
        self.drop_every = drop_every
        self.defects = defects or BODIES[tile_index % len(BODIES)]
        self.seed = seed

        self._connected = False
        self._dropped_at: set[int] = set()
        self._files: dict[str, bytes] = {}
        self._page_no = 0
        self._page_cache: tuple[int, np.ndarray] | None = None

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        time.sleep(0.05)
        self._connected = True
        self.last_error = ""

    def disconnect(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def model(self) -> str:
        return "Sony Alpha-A6000 (mock)"

    #: A real ILCE-6000's list, so the exposure calculator snaps to the same
    #: values against the mock as it will against the body.
    SHUTTER_CHOICES = [
        "30", "25", "20", "15", "13", "10", "8", "6", "5", "4", "32/10",
        "25/10", "2", "16/10", "13/10", "1", "8/10", "6/10", "5/10", "4/10",
        "1/3", "1/4", "1/5", "1/6", "1/8", "1/10", "1/13", "1/15", "1/20",
        "1/25", "1/30", "1/40", "1/50", "1/60", "1/80", "1/100", "1/125",
        "1/160", "1/200", "1/250", "1/320", "1/400", "1/500", "1/640",
        "1/800", "1/1000", "Bulb",
    ]

    def config_choices(self, name: str) -> list[str]:
        if name == "shutterspeed":
            return list(self.SHUTTER_CHOICES)
        if name == "iso":
            return ["Auto ISO", "100", "200", "400", "800", "1600", "3200"]
        if name == "f-number":
            return ["f/3.5", "f/4", "f/5.6", "f/8", "f/11", "f/16"]
        return []

    def apply_settings(self, settings: CameraSettings) -> None:
        self._require_connection()
        time.sleep(0.02)
        self.settings = settings

    # -- internals ---------------------------------------------------------

    def _require_connection(self) -> None:
        if not self._connected:
            raise CameraDisconnected(f"{self.camera_id}: no PTP session")

    def _page(self, page_no: int) -> np.ndarray:
        if self._page_cache and self._page_cache[0] == page_no:
            return self._page_cache[1]
        page, _ = render_page(
            self.geom.document.long_mm,
            self.geom.document.short_mm,
            px_per_mm=self.page_px_per_mm,
            spread=True,
            seed=self.seed + page_no,
        )
        self._page_cache = (page_no, page)
        return page

    # -- operations --------------------------------------------------------

    def capture(self, seq: int, frames: int = 1) -> list[CapturedFile]:
        self._require_connection()
        # Drop the session once per trigger point, not every time we reach
        # it -- otherwise a correct retry would loop forever and the test
        # would be exercising the mock rather than the recovery path.
        if (
            self.drop_every
            and self.frames_captured
            and self.frames_captured % self.drop_every == 0
            and self.frames_captured not in self._dropped_at
        ):
            self._dropped_at.add(self.frames_captured)
            self._connected = False
            raise CameraDisconnected(
                f"{self.camera_id}: session dropped while idle (simulated)"
            )

        with self._lock:
            out: list[CapturedFile] = []
            page = self._page(self._page_no)
            for f in range(frames):
                t0 = time.perf_counter()
                img, _truth = simulate_capture(
                    page,
                    self.page_px_per_mm,
                    self.geom,
                    self.tile_index,
                    self.defects,
                    scale=self.scale,
                    seed=self.seed + seq * 17 + f,
                )
                ok, buf = cv2.imencode(".png", img)
                if not ok:
                    raise RuntimeError("mock encode failed")
                data = buf.tobytes()

                # Model the transfer, not the render: a real ARW is 24 MB
                # over a 15 MB/s link, and that is what bounds throughput.
                budget = self.file_mb / max(self.transfer_mb_s, 1e-6)
                elapsed = time.perf_counter() - t0
                if elapsed < budget:
                    time.sleep(budget - elapsed)

                fid = f"{self.camera_id}-{seq:06d}-{f}-{uuid.uuid4().hex[:6]}"
                self._files[fid] = data
                self.frames_captured += 1
                out.append(
                    CapturedFile(
                        file_id=fid,
                        camera_id=self.camera_id,
                        seq=seq,
                        frame_index=f,
                        size_bytes=len(data),
                        latency_s=round(time.perf_counter() - t0, 3),
                        content_type="image/png",
                    )
                )
            self._page_no += 1
            return out

    def read_file(self, file_id: str) -> bytes:
        if file_id not in self._files:
            raise KeyError(f"unknown file {file_id}")
        return self._files[file_id]

    def forget(self, file_id: str) -> None:
        self._files.pop(file_id, None)

    def preview(self) -> bytes:
        self._require_connection()
        page = self._page(self._page_no)
        img, _ = simulate_capture(
            page, self.page_px_per_mm, self.geom, self.tile_index,
            self.defects, scale=self.scale * 0.35, seed=self.seed,
        )
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            raise RuntimeError("preview encode failed")
        return buf.tobytes()
