"""
Focus scoring, for turning a lens ring.

MTF50 is the honest measure of sharpness, but it needs a slanted edge in
the frame.  While you are actually focusing you have a book, not a test
target, so this provides a content-agnostic score instead.

It is **relative, not absolute**.  The number means nothing on its own --
it only means something as you watch it change.  The scene must hold
still while you turn the ring, which it does.

Tenengrad (mean squared Sobel gradient) normalised by image variance:
the normalisation is what stops the score jumping when you pan across a
denser part of the page.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FocusRegion:
    name: str
    score: float
    #: Fraction of the peak seen so far, 0..1. Watch this, not `score`.
    relative: float = 1.0


def focus_score(image: np.ndarray) -> float:
    """Tenengrad, normalised. Higher is sharper, for a fixed scene."""
    g = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    g = g.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    energy = float(np.mean(gx * gx + gy * gy))
    var = float(np.var(g))
    return energy / max(var, 1.0)


#: Centre plus four corners.  Corners are what decide whether the kit lens
#: stays -- the centre of any lens at f/8 looks fine.
REGIONS = {
    "top left": (0.06, 0.06),
    "top right": (0.74, 0.06),
    "centre": (0.40, 0.40),
    "bottom left": (0.06, 0.74),
    "bottom right": (0.74, 0.74),
}


def focus_map(image: np.ndarray, frac: float = 0.20) -> list[FocusRegion]:
    """Score five regions of a frame."""
    h, w = image.shape[:2]
    rw, rh = int(w * frac), int(h * frac)
    out = []
    for name, (fx, fy) in REGIONS.items():
        x, y = int(w * fx), int(h * fy)
        roi = image[y:y + rh, x:x + rw]
        if roi.size:
            out.append(FocusRegion(name, focus_score(roi)))
    return out


def exposure_stats(image: np.ndarray) -> dict:
    """
    Clipping and level, so the GUI can warn before a whole book is shot
    two stops hot.
    """
    g = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    hist = cv2.calcHist([g], [0], None, [64], [0, 256]).ravel()
    total = max(g.size, 1)
    return {
        "mean": round(float(g.mean()), 1),
        "clipped_high_pct": round(100.0 * float((g >= 253).sum()) / total, 3),
        "clipped_low_pct": round(100.0 * float((g <= 2).sum()) / total, 3),
        "histogram": (hist / max(hist.max(), 1.0)).round(4).tolist(),
    }
