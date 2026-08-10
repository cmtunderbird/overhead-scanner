import numpy as np
import pytest

from scanner.geometry import RigGeometry, A3
from scanner.synth.page import render_page

#: Small enough to keep the suite fast, large enough to exercise every stage.
TEST_SCALE = 0.16
PAGE_PPM = 5.0


@pytest.fixture(scope="session")
def geom():
    return RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)


@pytest.fixture(scope="session")
def page(geom):
    return render_page(geom.document.long_mm, geom.document.short_mm,
                       px_per_mm=PAGE_PPM, spread=True, seed=3)
