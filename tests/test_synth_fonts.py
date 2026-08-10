"""
Font resolution for the synthetic page.

The mock camera renders its pages wherever the node happens to run, which
is a Windows or macOS laptop as often as a Linux Pi.  Hard-coding
`/usr/share/fonts` made `scanner gui --mock` fail on Windows with
"no TrueType fonts available for page synthesis" -- a rehearsal aid
refusing to rehearse because the machine ships Arial instead of
Liberation Sans.

Nothing measured on a synthetic page is drawn with a font: the slanted
edges, colour patches, fiducials and ruler are all geometry.  So a
substitute face changes the texture, never the ground truth, and falling
back is always better than raising.
"""
import numpy as np
import pytest

from scanner.synth import page as P


@pytest.fixture(autouse=True)
def clear_caches():
    for fn in (P._font_path, P._font, P._installed_fonts):
        fn.cache_clear()
    saved = P._FONT_DIRS, P._installed_fonts
    yield
    P._FONT_DIRS, P._installed_fonts = saved
    for fn in (P._font_path, P._font, P._installed_fonts):
        fn.cache_clear()


def _render():
    img, truth = P.render_page(297.0, 210.0, px_per_mm=2.0, spread=True, seed=3)
    return img, truth


def test_it_renders_with_whatever_is_installed():
    img, truth = _render()
    assert img.ndim == 3 and img.dtype == np.uint8
    assert truth.edges and truth.patches


def test_a_windows_font_set_resolves():
    """C:\\Windows\\Fonts has arial/times/cour, not the Liberation faces."""
    real = P._installed_fonts()
    sans = real[next(k for k in real)]           # any real face will do
    P._installed_fonts = lambda: {"arial": sans, "times": sans, "cour": sans}
    P._font_path.cache_clear()
    assert P._font_path(P._SANS) == sans
    assert P._font_path(P._SERIF) == sans
    assert P._font_path(P._MONO) == sans


def test_no_fonts_at_all_falls_back_rather_than_raising():
    P._installed_fonts = lambda: {}
    P._font_path.cache_clear()
    P._font.cache_clear()
    assert P._font_path(P._SANS) is None
    img, _ = _render()                            # must not raise
    assert img.shape[0] > 0


def test_an_unreadable_face_falls_back_rather_than_raising(monkeypatch):
    """A file that ends in .ttf but is not a font must not stop a render."""
    real_truetype = P.ImageFont.truetype

    def refuse(font=None, *a, **k):
        # load_default() also goes through truetype -- only reject ours
        if isinstance(font, str) and font.endswith("NotAFont.ttf"):
            raise OSError("cannot open resource")
        return real_truetype(font, *a, **k)

    P._installed_fonts = lambda: {"notafont": "/nowhere/NotAFont.ttf"}
    P._font_path.cache_clear()
    P._font.cache_clear()
    monkeypatch.setattr(P.ImageFont, "truetype", refuse)

    assert P._font_path(P._SANS) == "/nowhere/NotAFont.ttf"
    assert P._font(P._SANS, 12.0) is not None     # OSError swallowed
    img, _ = _render()
    assert img.shape[0] > 0


def test_the_ground_truth_does_not_depend_on_the_font():
    """The measurable geometry is drawn with primitives, not glyphs."""
    _, with_fonts = _render()
    P._installed_fonts = lambda: {}
    P._font_path.cache_clear()
    P._font.cache_clear()
    _, without = _render()

    assert [(e.cx_mm, e.cy_mm, e.angle_deg) for e in with_fonts.edges] == \
           [(e.cx_mm, e.cy_mm, e.angle_deg) for e in without.edges]
    assert with_fonts.ruler_ends_mm == without.ruler_ends_mm
    assert with_fonts.spine_mm == without.spine_mm
