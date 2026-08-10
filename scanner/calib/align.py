"""
Document alignment: where each camera's frame sits on the page.

This is the step that replaces runtime feature-based stitching.  You lay
a ChArUco board flat on the platen at a known position, shoot it once
with each camera, and solve the homography from document millimetres to
undistorted image pixels.  After that, stitching is a fixed warp -- no
feature matching, no per-page variation, no seams that wander.

Because the homography is solved in *millimetres*, the two cameras end up
in a shared metric frame automatically.  There is no separate
"extrinsics" step and no scale ambiguity.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .intrinsics import BoardSpec, A4_BOARD, detect_board, undistort_points
from .store import CameraCalibration


@dataclass
class BoardPlacement:
    """
    Where the calibration board physically sits on the platen.

    `origin_mm` is the document coordinate of the board's own (0,0)
    chessboard corner.  `rotation_deg` allows the board to be laid down
    turned; 0 means the board's x axis runs along the document long axis.
    """

    origin_mm: tuple[float, float] = (0.0, 0.0)
    rotation_deg: float = 0.0

    def board_to_doc(self, pts_mm: np.ndarray) -> np.ndarray:
        th = np.radians(self.rotation_deg)
        R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
        return pts_mm @ R.T + np.array(self.origin_mm)


@dataclass
class AlignmentResult:
    H_doc_to_img: np.ndarray
    #: RMS residual of the fit, in image pixels.
    rms_px: float
    n_points: int
    #: Document-mm extent actually covered by the detected points.
    covered_mm: tuple[float, float, float, float]


def solve_document_homography(
    image: np.ndarray,
    calib: CameraCalibration,
    placement: BoardPlacement = BoardPlacement(),
    spec: BoardSpec = A4_BOARD,
    *,
    min_corners: int = 12,
) -> AlignmentResult:
    """
    Solve document mm -> undistorted image px for one camera.

    The image may be raw (distorted); the detected corners are undistorted
    analytically, which is more accurate than undistorting the whole frame
    first and then detecting in a resampled image.
    """
    det = detect_board(image, spec, min_corners=min_corners)
    if det is None:
        raise ValueError(
            "no usable board detected -- is the board flat on the platen, "
            "fully lit, and inside this camera's tile?"
        )

    board_pts = spec.board().getChessboardCorners()[det.ids.ravel()][:, :2]
    doc_pts = placement.board_to_doc(board_pts.astype(np.float64))
    img_pts = undistort_points(det.corners.reshape(-1, 2), calib)

    H, mask = cv2.findHomography(
        doc_pts.astype(np.float64), img_pts.astype(np.float64),
        method=cv2.RANSAC, ransacReprojThreshold=2.0,
    )
    if H is None:
        raise ValueError("homography solve failed; too few or degenerate points")

    proj = cv2.perspectiveTransform(doc_pts.reshape(-1, 1, 2), H).reshape(-1, 2)
    resid = np.linalg.norm(proj - img_pts, axis=1)
    rms = float(np.sqrt((resid**2).mean()))

    calib.H_doc_to_img = H
    return AlignmentResult(
        H_doc_to_img=H,
        rms_px=rms,
        n_points=int(len(doc_pts)),
        covered_mm=(
            float(doc_pts[:, 0].min()), float(doc_pts[:, 1].min()),
            float(doc_pts[:, 0].max()), float(doc_pts[:, 1].max()),
        ),
    )


def homography_from_tile(
    calib: CameraCalibration,
    tile_x0_mm: float,
    tile_x1_mm: float,
    doc_short_mm: float,
) -> np.ndarray:
    """
    Nominal homography for a perfectly mounted camera.

    A fallback and a sanity reference -- if the solved homography differs
    from this by more than a few millimetres, something is mounted wrong
    and no amount of software will make the seam behave.
    """
    w, h = calib.image_size
    ppm_x = w / (tile_x1_mm - tile_x0_mm)
    cx_mm = 0.5 * (tile_x0_mm + tile_x1_mm)
    cy_mm = doc_short_mm / 2.0
    return np.array(
        [
            [ppm_x, 0.0, w / 2.0 - ppm_x * cx_mm],
            [0.0, ppm_x, h / 2.0 - ppm_x * cy_mm],
            [0.0, 0.0, 1.0],
        ]
    )


def mounting_error(
    calib: CameraCalibration, doc_short_mm: float
) -> dict:
    """
    Decompose the solved homography into human-readable mounting error:
    how far off centre, how rotated, how far off the intended scale.

    Numbers you can act on with an Allen key, rather than a 3x3 matrix.
    """
    if calib.H_doc_to_img is None:
        raise ValueError("camera not aligned yet")
    nominal = homography_from_tile(
        calib, calib.tile_x0_mm, calib.tile_x1_mm, doc_short_mm
    )
    D = np.linalg.inv(nominal) @ calib.H_doc_to_img  # residual, in doc mm
    a, b = D[0, 0], D[0, 1]
    c, d = D[1, 0], D[1, 1]
    scale = float(np.sqrt(abs(a * d - b * c)))
    rot = float(np.degrees(np.arctan2(c, a)))
    return {
        "dx_mm": float(D[0, 2]),
        "dy_mm": float(D[1, 2]),
        "rotation_deg": rot,
        "scale_error_pct": (scale - 1.0) * 100.0,
        "keystone": float(np.hypot(D[2, 0], D[2, 1])),
    }
