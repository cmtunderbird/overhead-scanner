"""
Flat-field and colour calibration.

These are the two unglamorous steps that decide whether a stitched spread
looks like one photograph or two.  Vignetting leaves a dark band where
the tiles join; an unmatched body leaves a colour band in the same place.
Both are measured once and applied forever.
"""

from __future__ import annotations

import cv2
import numpy as np

from ..metrics.colour import srgb_to_linear, linear_to_srgb
from .store import CameraCalibration


def build_flat_field(
    blank_shots: list[np.ndarray] | np.ndarray,
    *,
    smooth_sigma_frac: float = 0.04,
    clip: float = 4.0,
) -> np.ndarray:
    """
    Build a multiplicative gain map from one or more shots of an evenly
    lit blank sheet at the working height.

    The map is heavily smoothed on purpose: we want the lens and lamp
    falloff, not the dust on the sheet.  Normalised so the frame centre
    is gain 1.0, which keeps exposure unchanged.
    """
    shots = [blank_shots] if isinstance(blank_shots, np.ndarray) else list(blank_shots)
    if not shots:
        raise ValueError("no blank-sheet shots supplied")

    stack = np.mean([s.astype(np.float32) for s in shots], axis=0)
    if stack.ndim == 2:
        stack = stack[..., None]
    h, w = stack.shape[:2]

    sigma = smooth_sigma_frac * max(h, w)
    smooth = cv2.GaussianBlur(stack, (0, 0), sigma)
    smooth = np.maximum(smooth, 1e-3)

    ch, cw = h // 2, w // 2
    centre = smooth[
        max(0, ch - h // 20) : ch + h // 20 + 1,
        max(0, cw - w // 20) : cw + w // 20 + 1,
    ].mean(axis=(0, 1))

    gain = centre[None, None, :] / smooth
    return np.clip(gain, 1.0 / clip, clip).astype(np.float32)


def apply_flat_field(image: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """Apply a gain map, resizing it if the image was scaled."""
    if gain is None:
        return image
    if gain.shape[:2] != image.shape[:2]:
        gain = cv2.resize(
            gain, (image.shape[1], image.shape[0]), interpolation=cv2.INTER_LINEAR
        )
    if gain.ndim == 2:
        gain = gain[..., None]
    out = image.astype(np.float32) * gain
    return np.clip(out, 0, 255).astype(image.dtype)


def fit_colour_matrix(
    measured_rgb: np.ndarray,
    reference_rgb: np.ndarray,
    *,
    linearise: bool = True,
) -> np.ndarray:
    """
    Least-squares 3x3 mapping measured -> reference.

    Fitted in *linear* light.  Fitting in gamma space is a common and
    silent mistake: the matrix then depends on exposure, so it stops
    working the moment you change the lamps.
    """
    m = np.asarray(measured_rgb, dtype=np.float64)
    r = np.asarray(reference_rgb, dtype=np.float64)
    if m.shape != r.shape or m.ndim != 2 or m.shape[1] != 3:
        raise ValueError("measured and reference must both be (N,3)")
    if m.max() > 1.0 + 1e-9:
        m = m / 255.0
    if r.max() > 1.0 + 1e-9:
        r = r / 255.0
    if linearise:
        m, r = srgb_to_linear(m), srgb_to_linear(r)

    M, *_ = np.linalg.lstsq(m, r, rcond=None)
    return M.T  # so that corrected = M @ rgb


_ENCODE_STEPS = 4096


def _decode_lut() -> np.ndarray:
    """8-bit sRGB -> linear."""
    return srgb_to_linear(np.arange(256) / 255.0).astype(np.float32)


def _encode_lut() -> np.ndarray:
    """linear (quantised to 12 bits) -> 8-bit sRGB."""
    x = np.arange(_ENCODE_STEPS) / (_ENCODE_STEPS - 1)
    return np.clip(linear_to_srgb(x) * 255.0 + 0.5, 0, 255).astype(np.uint8)


_DECODE = _decode_lut()
_ENCODE = _encode_lut()


def apply_colour_matrix(
    image_bgr: np.ndarray, M: np.ndarray | None, *, linearise: bool = True
) -> np.ndarray:
    """
    Apply a 3x3 colour correction to a BGR image.

    Both transfer functions go through lookup tables -- 8 bits in, 12 bits
    out.  Calling pow() on a 40-megapixel array costs seconds per spread
    and buys precision that an 8-bit output cannot carry.
    """
    if M is None:
        return image_bgr
    a = np.ascontiguousarray(image_bgr[..., ::-1])  # -> RGB
    lin = _DECODE[a] if linearise else (a.astype(np.float32) / 255.0)
    out = lin @ np.asarray(M, dtype=np.float32).T
    if linearise:
        idx = np.clip(out * (_ENCODE_STEPS - 1), 0, _ENCODE_STEPS - 1).astype(np.uint16)
        rgb = _ENCODE[idx]
    else:
        rgb = np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(rgb[..., ::-1])


def calibrate_colour(
    image_bgr: np.ndarray,
    patch_rois: list[tuple[int, int, int, int]],
    reference_srgb: list[tuple[int, int, int]],
    calib: CameraCalibration | None = None,
) -> np.ndarray:
    """
    Measure a colour chart in this camera's frame and fit its correction
    matrix.  Attaches it to `calib` when one is given.
    """
    from ..metrics.colour import sample_patch

    measured = np.array([sample_patch(image_bgr, *r)[::-1] for r in patch_rois])
    M = fit_colour_matrix(measured, np.array(reference_srgb, dtype=np.float64))
    if calib is not None:
        calib.colour_matrix = M
    return M


def calibrate_flat_field(
    blank_shots: list[np.ndarray] | np.ndarray,
    calib: CameraCalibration | None = None,
    **kwargs,
) -> np.ndarray:
    gain = build_flat_field(blank_shots, **kwargs)
    if calib is not None:
        calib.flat_field = gain
    return gain


def exposure_offset(image_a: np.ndarray, image_b: np.ndarray) -> float:
    """
    Ratio of mean luminance between two frames of the same scene.

    Useful as a quick check that the two bodies are actually metering the
    same: anything beyond a few percent and you have a settings mismatch,
    not a calibration problem.
    """
    ga = cv2.cvtColor(image_a, cv2.COLOR_BGR2GRAY).astype(np.float64).mean()
    gb = cv2.cvtColor(image_b, cv2.COLOR_BGR2GRAY).astype(np.float64).mean()
    return float(ga / max(gb, 1e-6))
