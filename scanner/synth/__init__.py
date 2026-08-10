"""Synthetic capture: pages and cameras that exist only in software."""

from .page import render_page, render_charuco, PageTruth, COLORCHECKER_SRGB
from .camera import (
    CameraDefects, CaptureTruth, BoardPose, BODY_A, BODY_B,
    simulate_capture, simulate_rig, simulate_flat_field,
    simulate_board_capture, default_calibration_poses,
)
