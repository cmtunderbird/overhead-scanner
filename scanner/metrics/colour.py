"""
Colour metrics: sRGB -> CIELAB and CIEDE2000.

The blueprint's dE < 2 between the two bodies is not a nicety.  Two
A6000s of the same model differ by a few percent per channel, and on a
stitched spread that difference lands as a visible vertical band down
the middle of every page.  It is the most commonly botched step in
twin-camera rigs, and the only defence is measuring it.
"""

from __future__ import annotations

import numpy as np

# D65 white point, 2 degree observer.
_WHITE = np.array([0.95047, 1.00000, 1.08883])

_M_SRGB_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ]
)


def srgb_to_linear(srgb: np.ndarray) -> np.ndarray:
    """Undo the sRGB transfer function. Input 0..1."""
    a = np.asarray(srgb, dtype=np.float64)
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(lin: np.ndarray) -> np.ndarray:
    a = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    return np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)


def srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """
    sRGB (0..255 or 0..1, last axis RGB) -> CIELAB.
    """
    a = np.asarray(rgb, dtype=np.float64)
    if a.max() > 1.0 + 1e-9:
        a = a / 255.0
    lin = srgb_to_linear(a)
    xyz = lin @ _M_SRGB_TO_XYZ.T
    xyz = xyz / _WHITE

    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0)
    fx, fy, fz = f[..., 0], f[..., 1], f[..., 2]
    return np.stack(
        [116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=-1
    )


def bgr_to_lab(bgr: np.ndarray) -> np.ndarray:
    """Convenience for OpenCV-ordered arrays."""
    return srgb_to_lab(np.asarray(bgr)[..., ::-1])


def delta_e_2000(lab1: np.ndarray, lab2: np.ndarray) -> np.ndarray:
    """
    CIEDE2000 colour difference.

    Roughly: dE 1 is a just-noticeable difference under ideal viewing,
    dE 2 is noticeable side by side, dE 5 is obvious.
    """
    lab1 = np.asarray(lab1, dtype=np.float64)
    lab2 = np.asarray(lab2, dtype=np.float64)
    L1, a1, b1 = lab1[..., 0], lab1[..., 1], lab1[..., 2]
    L2, a2, b2 = lab2[..., 0], lab2[..., 1], lab2[..., 2]

    C1 = np.hypot(a1, b1)
    C2 = np.hypot(a2, b2)
    Cbar = 0.5 * (C1 + C2)
    G = 0.5 * (1.0 - np.sqrt(Cbar**7 / (Cbar**7 + 25.0**7 + 1e-30)))

    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360.0
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360.0

    dLp = L2 - L1
    dCp = C2p - C1p

    dhp = h2p - h1p
    dhp = np.where(dhp > 180.0, dhp - 360.0, dhp)
    dhp = np.where(dhp < -180.0, dhp + 360.0, dhp)
    dhp = np.where(C1p * C2p == 0.0, 0.0, dhp)
    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2.0))

    Lbar = 0.5 * (L1 + L2)
    Cbarp = 0.5 * (C1p + C2p)

    hsum = h1p + h2p
    hdiff = np.abs(h1p - h2p)
    hbar = np.where(
        C1p * C2p == 0.0,
        hsum,
        np.where(
            hdiff <= 180.0,
            0.5 * hsum,
            np.where(hsum < 360.0, 0.5 * (hsum + 360.0), 0.5 * (hsum - 360.0)),
        ),
    )

    T = (
        1.0
        - 0.17 * np.cos(np.radians(hbar - 30.0))
        + 0.24 * np.cos(np.radians(2.0 * hbar))
        + 0.32 * np.cos(np.radians(3.0 * hbar + 6.0))
        - 0.20 * np.cos(np.radians(4.0 * hbar - 63.0))
    )
    dtheta = 30.0 * np.exp(-(((hbar - 275.0) / 25.0) ** 2))
    Rc = 2.0 * np.sqrt(Cbarp**7 / (Cbarp**7 + 25.0**7 + 1e-30))
    Sl = 1.0 + (0.015 * (Lbar - 50.0) ** 2) / np.sqrt(20.0 + (Lbar - 50.0) ** 2)
    Sc = 1.0 + 0.045 * Cbarp
    Sh = 1.0 + 0.015 * Cbarp * T
    Rt = -np.sin(np.radians(2.0 * dtheta)) * Rc

    return np.sqrt(
        (dLp / Sl) ** 2
        + (dCp / Sc) ** 2
        + (dHp / Sh) ** 2
        + Rt * (dCp / Sc) * (dHp / Sh)
    )


def sample_patch(
    image: np.ndarray, x: int, y: int, w: int, h: int, inset: float = 0.30
) -> np.ndarray:
    """
    Robust mean colour of a patch: trim the border, then take the median
    so a dust speck or a specular glint cannot move the answer.
    """
    dx, dy = int(w * inset), int(h * inset)
    roi = image[y + dy : y + h - dy, x + dx : x + w - dx]
    if roi.size == 0:
        raise ValueError("empty patch roi")
    return np.median(roi.reshape(-1, roi.shape[-1]), axis=0)


def chart_delta_e(
    image_bgr: np.ndarray,
    rois: list[tuple[int, int, int, int]],
    reference_srgb: list[tuple[int, int, int]],
) -> dict:
    """
    Measure a rendered colour chart against its reference values.

    Returns mean / median / max / p95 dE2000 plus the per-patch vector.
    """
    if len(rois) != len(reference_srgb):
        raise ValueError("roi count must match reference count")
    measured = np.array(
        [sample_patch(image_bgr, *r)[::-1] for r in rois]  # BGR -> RGB
    )
    ref = np.array(reference_srgb, dtype=np.float64)
    de = delta_e_2000(srgb_to_lab(measured), srgb_to_lab(ref))
    return {
        "mean": float(np.mean(de)),
        "median": float(np.median(de)),
        "p95": float(np.percentile(de, 95)),
        "max": float(np.max(de)),
        "per_patch": de.tolist(),
        "measured_rgb": measured.tolist(),
    }


def cross_camera_delta_e(
    image_a: np.ndarray,
    rois_a: list[tuple[int, int, int, int]],
    image_b: np.ndarray,
    rois_b: list[tuple[int, int, int, int]],
) -> dict:
    """
    The number that actually matters for a twin-camera rig: how different
    do the two bodies render the *same* colours?  This is the seam.
    """
    if len(rois_a) != len(rois_b):
        raise ValueError("roi counts must match")
    ma = np.array([sample_patch(image_a, *r)[::-1] for r in rois_a])
    mb = np.array([sample_patch(image_b, *r)[::-1] for r in rois_b])
    de = delta_e_2000(srgb_to_lab(ma), srgb_to_lab(mb))
    return {
        "mean": float(np.mean(de)),
        "median": float(np.median(de)),
        "p95": float(np.percentile(de, 95)),
        "max": float(np.max(de)),
        "per_patch": de.tolist(),
    }
