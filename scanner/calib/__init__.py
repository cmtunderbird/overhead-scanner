"""Calibration: intrinsics, alignment, flat-field, colour."""

from .store import CameraCalibration, RigCalibration
from .intrinsics import BoardSpec, A4_BOARD, calibrate_intrinsics, detect_board, undistort
from .align import BoardPlacement, solve_document_homography, mounting_error
from .field import (
    build_flat_field, apply_flat_field, fit_colour_matrix,
    apply_colour_matrix, calibrate_colour, calibrate_flat_field,
)
