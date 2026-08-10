"""
The pipeline runner: raw frames in, archival master out.

Everything here is deterministic and metric.  Super-resolution and OCR
are deliberately *not* part of this module -- the master is written
before either is invoked, and the two are never conflated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import cv2
import numpy as np

from ..calib.store import RigCalibration
from . import stages


@dataclass
class PipelineConfig:
    deskew: bool = True
    crop: bool = True
    normalise_background: bool = True
    split_spread: bool = True
    feather_px: int = 60
    max_skew_deg: float = 3.0
    gutter_trim_px: int = 0
    #: Write intermediates alongside the master. Slow; for debugging.
    debug_dir: str | None = None


@dataclass
class SpreadResult:
    """One processed spread."""

    master: np.ndarray
    pages: list[np.ndarray] = field(default_factory=list)
    coverage: np.ndarray | None = None
    report: dict = field(default_factory=dict)

    def save(self, out_dir: str | Path, stem: str, *, master_ext: str = ".tif") -> dict:
        """
        Write the archival master losslessly and the pages beside it.

        TIFF for the master: lossless, widely readable, and it will still
        open in thirty years.  That is the whole point of a master.
        """
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = {}
        mp = out / f"{stem}_master{master_ext}"
        cv2.imwrite(str(mp), self.master)
        paths["master"] = str(mp)
        for i, pg in enumerate(self.pages):
            pp = out / f"{stem}_p{i + 1}{master_ext}"
            cv2.imwrite(str(pp), pg)
            paths[f"page{i + 1}"] = str(pp)
        return paths


def process_spread(
    frames: dict[str, np.ndarray],
    rig: RigCalibration,
    config: PipelineConfig | None = None,
) -> SpreadResult:
    """
    Run the full deterministic pipeline on one set of simultaneous frames.

    `frames` maps camera_id -> raw image.  Cameras missing from `frames`
    are skipped, so a one-camera rig runs the identical code path as a
    two-camera one -- which is the point: nothing changes when the second
    body arrives.
    """
    cfg = config or PipelineConfig()
    t0 = time.perf_counter()
    report: dict = {"cameras": {}, "config": asdict(cfg)}

    cams = [c for c in rig.ordered() if c.camera_id in frames]
    if not cams:
        raise ValueError(
            f"none of the calibrated cameras {list(rig.cameras)} "
            f"are present in this capture {list(frames)}"
        )
    missing = [c.camera_id for c in rig.ordered() if c.camera_id not in frames]
    if missing:
        report["missing_cameras"] = missing

    canvas = rig.canvas_size
    dbg = Path(cfg.debug_dir) if cfg.debug_dir else None
    if dbg:
        dbg.mkdir(parents=True, exist_ok=True)

    # -- 1. per-camera correction + rectification --------------------------
    warped, masks = [], []
    for c in cams:
        t = time.perf_counter()
        corrected = stages.correct_frame(frames[c.camera_id], c)
        w, m = stages.rectify_to_document(
            corrected, c, canvas, rig.target_px_per_mm
        )
        warped.append(w)
        masks.append(m)
        report["cameras"][c.camera_id] = {
            "tile_mm": [c.tile_x0_mm, c.tile_x1_mm],
            "coverage_pct": round(100.0 * float((m > 0).mean()), 2),
            "ms": round(1000 * (time.perf_counter() - t), 1),
        }
        if dbg:
            cv2.imwrite(str(dbg / f"01_rect_{c.camera_id}.jpg"), w)

    # -- 2. stitch ---------------------------------------------------------
    t = time.perf_counter()
    image, coverage = stages.blend_tiles(warped, masks, feather_px=cfg.feather_px)
    report["stitch_ms"] = round(1000 * (time.perf_counter() - t), 1)
    report["canvas_px"] = list(canvas)
    report["coverage_pct"] = round(100.0 * float((coverage > 0).mean()), 2)
    if dbg:
        cv2.imwrite(str(dbg / "02_stitched.jpg"), image)

    # -- 3. deskew ---------------------------------------------------------
    if cfg.deskew:
        t = time.perf_counter()
        angle = stages.estimate_skew(image, max_deg=cfg.max_skew_deg)
        image = stages.rotate(image, angle)
        coverage = stages.rotate(coverage, angle)
        report["skew_deg"] = round(angle, 3)
        report["deskew_ms"] = round(1000 * (time.perf_counter() - t), 1)

    # -- 4. crop -----------------------------------------------------------
    if cfg.crop:
        image, box = stages.crop_to_content(image, coverage)
        coverage = coverage[box[1] : box[1] + box[3], box[0] : box[0] + box[2]]
        report["crop_box"] = list(box)

    # -- 5. background normalisation ---------------------------------------
    if cfg.normalise_background:
        t = time.perf_counter()
        image = stages.normalise_background(image)
        report["normalise_ms"] = round(1000 * (time.perf_counter() - t), 1)
        if dbg:
            cv2.imwrite(str(dbg / "03_normalised.jpg"), image)

    # -- 6. split ----------------------------------------------------------
    pages: list[np.ndarray] = []
    if cfg.split_spread:
        spine = stages.detect_spine(image)
        report["spine"] = {
            "x_px": spine.x_px,
            "x_mm": round(spine.x_px / rig.target_px_per_mm, 2),
            "confidence": round(spine.confidence, 4),
            "fallback": spine.fallback,
        }
        pages = stages.split_spread(image, spine.x_px, cfg.gutter_trim_px)
    else:
        pages = [image]

    report["total_ms"] = round(1000 * (time.perf_counter() - t0), 1)
    report["output_px"] = [image.shape[1], image.shape[0]]
    report["dpi"] = round(rig.dpi, 1)
    report["megapixels"] = round(image.shape[0] * image.shape[1] / 1e6, 2)

    return SpreadResult(master=image, pages=pages, coverage=coverage, report=report)


def process_session(
    captures: list[dict[str, np.ndarray]],
    rig: RigCalibration,
    out_dir: str | Path,
    config: PipelineConfig | None = None,
    *,
    stem: str = "spread",
    progress: bool = True,
) -> dict:
    """Process a whole book's worth of captures and write a manifest."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"count": len(captures), "spreads": []}
    t0 = time.perf_counter()

    for i, frames in enumerate(captures):
        result = process_spread(frames, rig, config)
        paths = result.save(out, f"{stem}_{i:04d}")
        manifest["spreads"].append({"index": i, "paths": paths, **result.report})
        if progress:
            print(
                f"  [{i + 1}/{len(captures)}] {result.report['total_ms']:.0f} ms  "
                f"{result.report['megapixels']} MP"
            )

    manifest["elapsed_s"] = round(time.perf_counter() - t0, 2)
    if captures:
        manifest["mean_ms"] = round(
            float(np.mean([s["total_ms"] for s in manifest["spreads"]])), 1
        )
    (out / "manifest.json").write_text(
        __import__("json").dumps(manifest, indent=2)
    )
    return manifest
