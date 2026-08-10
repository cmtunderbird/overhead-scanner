"""
Calibration containers and on-disk format.

A calibration is only useful if it survives a reboot and can be diffed
against last month's.  Everything here serialises to plain JSON (plus a
sidecar .npz for the flat-field maps, which are too big for JSON).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


@dataclass
class CameraCalibration:
    """Everything known about one body + lens + its place on the rig."""

    camera_id: str
    image_size: tuple[int, int]  # (width, height)
    #: Pinhole camera matrix, 3x3.
    K: np.ndarray
    #: Brown-Conrady coefficients (k1,k2,p1,p2,k3).
    dist: np.ndarray
    #: RMS reprojection error from the intrinsics solve, px.
    reprojection_rms: float = float("nan")
    #: Multiplicative flat-field gain map, same size as the image, or None.
    flat_field: np.ndarray | None = None
    #: 3x3 linear colour correction applied to linear RGB, or None.
    colour_matrix: np.ndarray | None = None
    #: Undistorted-image px <- document mm homography, 3x3, or None.
    H_doc_to_img: np.ndarray | None = None
    #: Tile extent on the document long axis, mm.
    tile_x0_mm: float = 0.0
    tile_x1_mm: float = 0.0
    notes: str = ""

    @property
    def H_img_to_doc(self) -> np.ndarray:
        if self.H_doc_to_img is None:
            raise ValueError(f"{self.camera_id}: no document homography yet")
        return np.linalg.inv(self.H_doc_to_img)

    def to_dict(self) -> dict:
        return {
            "camera_id": self.camera_id,
            "image_size": list(self.image_size),
            "K": np.asarray(self.K).tolist(),
            "dist": np.asarray(self.dist).ravel().tolist(),
            "reprojection_rms": float(self.reprojection_rms),
            "colour_matrix": (
                np.asarray(self.colour_matrix).tolist()
                if self.colour_matrix is not None
                else None
            ),
            "H_doc_to_img": (
                np.asarray(self.H_doc_to_img).tolist()
                if self.H_doc_to_img is not None
                else None
            ),
            "tile_x0_mm": self.tile_x0_mm,
            "tile_x1_mm": self.tile_x1_mm,
            "has_flat_field": self.flat_field is not None,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict, flat_field: np.ndarray | None = None):
        return cls(
            camera_id=d["camera_id"],
            image_size=tuple(d["image_size"]),
            K=np.array(d["K"], dtype=np.float64),
            dist=np.array(d["dist"], dtype=np.float64),
            reprojection_rms=d.get("reprojection_rms", float("nan")),
            flat_field=flat_field,
            colour_matrix=(
                np.array(d["colour_matrix"], dtype=np.float64)
                if d.get("colour_matrix") is not None
                else None
            ),
            H_doc_to_img=(
                np.array(d["H_doc_to_img"], dtype=np.float64)
                if d.get("H_doc_to_img") is not None
                else None
            ),
            tile_x0_mm=d.get("tile_x0_mm", 0.0),
            tile_x1_mm=d.get("tile_x1_mm", 0.0),
            notes=d.get("notes", ""),
        )


@dataclass
class RigCalibration:
    """The rig as a whole: cameras plus the document canvas they share."""

    cameras: dict[str, CameraCalibration] = field(default_factory=dict)
    doc_long_mm: float = 420.0
    doc_short_mm: float = 297.0
    target_px_per_mm: float = 18.18
    overlap_mm: float = 20.0
    created: str = ""
    #: Free-form record of how this calibration was produced.
    provenance: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.created:
            self.created = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @property
    def canvas_size(self) -> tuple[int, int]:
        """Stitched output size in pixels, (width, height)."""
        return (
            int(round(self.doc_long_mm * self.target_px_per_mm)),
            int(round(self.doc_short_mm * self.target_px_per_mm)),
        )

    @property
    def dpi(self) -> float:
        return self.target_px_per_mm * 25.4

    def ordered(self) -> list[CameraCalibration]:
        """Cameras sorted left to right across the document."""
        return sorted(self.cameras.values(), key=lambda c: c.tile_x0_mm)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": 1,
            "created": self.created,
            "doc_long_mm": self.doc_long_mm,
            "doc_short_mm": self.doc_short_mm,
            "target_px_per_mm": self.target_px_per_mm,
            "overlap_mm": self.overlap_mm,
            "dpi": round(self.dpi, 2),
            "provenance": self.provenance,
            "cameras": {k: c.to_dict() for k, c in self.cameras.items()},
        }
        path.write_text(json.dumps(doc, indent=2))

        maps = {
            f"flat_{k}": c.flat_field
            for k, c in self.cameras.items()
            if c.flat_field is not None
        }
        if maps:
            np.savez_compressed(path.with_suffix(".npz"), **maps)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "RigCalibration":
        path = Path(path)
        doc = json.loads(path.read_text())
        npz_path = path.with_suffix(".npz")
        maps = dict(np.load(npz_path)) if npz_path.exists() else {}
        cams = {
            k: CameraCalibration.from_dict(v, maps.get(f"flat_{k}"))
            for k, v in doc["cameras"].items()
        }
        return cls(
            cameras=cams,
            doc_long_mm=doc["doc_long_mm"],
            doc_short_mm=doc["doc_short_mm"],
            target_px_per_mm=doc["target_px_per_mm"],
            overlap_mm=doc.get("overlap_mm", 20.0),
            created=doc.get("created", ""),
            provenance=doc.get("provenance", {}),
        )

    def summary(self) -> str:
        lines = [
            f"rig calibration  {self.created}",
            f"  canvas   {self.canvas_size[0]} x {self.canvas_size[1]} px "
            f"@ {self.dpi:.1f} DPI",
            f"  document {self.doc_long_mm:g} x {self.doc_short_mm:g} mm, "
            f"overlap {self.overlap_mm:g} mm",
        ]
        for c in self.ordered():
            bits = [
                f"rms {c.reprojection_rms:.3f} px" if np.isfinite(c.reprojection_rms) else "rms n/a",
                "flat" if c.flat_field is not None else "-",
                "colour" if c.colour_matrix is not None else "-",
                "aligned" if c.H_doc_to_img is not None else "NOT ALIGNED",
            ]
            lines.append(
                f"  {c.camera_id:<10} tile {c.tile_x0_mm:6.1f}-{c.tile_x1_mm:6.1f} mm   "
                + "  ".join(bits)
            )
        return "\n".join(lines)
