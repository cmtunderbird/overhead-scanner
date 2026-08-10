"""
Optical geometry for the overhead scanner rig.

This module is the single source of truth for every number in the build.
The drawings, the calibration targets and the acceptance thresholds all
derive from here -- nothing downstream should hard-code a DPI or a
working distance.

Blueprint v1.0 reference point (2 cameras, 20 mm overlap):
    tile width      220.0 mm
    DPI             461.8
    spread          41.2 MP
    working dist.   453.1 mm
    DOF @ f/8       22.8 mm
    stereo dz       26.7 um
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

Orientation = Literal["auto", "portrait", "landscape"]
Coverage = Literal["tile", "stereo"]

# Circle of confusion for depth-of-field, in pixels.
#
# The conventional APS-C CoC (19 um, ~4.9 px) is a print-viewing criterion and
# is far too loose here -- it would claim ~64 mm of depth of field.  A document
# scanner is judged at the pixel level, so we use a criterion just under two
# pixels.  This is the number the blueprint's +/-11 mm band comes from.
DEFAULT_COC_PX = 1.72

# Reference wavelength for the diffraction limit (green, mid-visible).
LAMBDA_MM = 550e-6


@dataclass(frozen=True)
class Sensor:
    """A camera sensor. `long`/`short` refer to the physical sensor axes."""

    name: str
    long_px: int
    short_px: int
    long_mm: float
    short_mm: float

    @property
    def pitch_long_mm(self) -> float:
        return self.long_mm / self.long_px

    @property
    def pitch_short_mm(self) -> float:
        return self.short_mm / self.short_px

    @property
    def megapixels(self) -> float:
        return self.long_px * self.short_px / 1e6


@dataclass(frozen=True)
class Lens:
    focal_mm: float
    f_number: float


@dataclass(frozen=True)
class DocumentFormat:
    """The thing being photographed. `long_mm` is the axis that gets split."""

    name: str
    long_mm: float
    short_mm: float


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

SONY_A6000 = Sensor(
    name="Sony A6000 (IMX)",
    long_px=6000,
    short_px=4000,
    long_mm=23.5,
    short_mm=15.6,
)

A3 = DocumentFormat("A3 spread", 420.0, 297.0)
A4 = DocumentFormat("A4", 297.0, 210.0)
A4_PORTRAIT = DocumentFormat("A4 portrait", 210.0, 297.0)
LETTER = DocumentFormat("US Letter", 279.4, 215.9)

FORMATS = {f.name: f for f in (A3, A4, A4_PORTRAIT, LETTER)}


@dataclass(frozen=True)
class Tile:
    """One camera's share of the document, in document millimetres."""

    index: int
    x0_mm: float
    x1_mm: float

    @property
    def width_mm(self) -> float:
        return self.x1_mm - self.x0_mm

    @property
    def centre_mm(self) -> float:
        return 0.5 * (self.x0_mm + self.x1_mm)


@dataclass(frozen=True)
class RigGeometry:
    """
    The complete optical solution for a given rig configuration.

    Every derived quantity is a property, so changing `overlap_mm` or
    `n_cameras` re-solves the whole thing consistently.
    """

    sensor: Sensor = SONY_A6000
    lens: Lens = Lens(focal_mm=30.0, f_number=8.0)
    document: DocumentFormat = A3
    n_cameras: int = 2
    overlap_mm: float = 20.0
    #: "tile"   -- each camera takes a slice; maximum resolution, no depth.
    #: "stereo" -- every camera sees the whole document; the baseline stops
    #:             being tied to the tiling and becomes a free parameter,
    #:             which is what makes 3-D page recovery possible.
    coverage: Coverage = "tile"
    #: Camera separation in stereo coverage, mm. Ignored when tiling.
    stereo_baseline_mm: float = 200.0
    #: Extra field kept beyond the document, as a fraction, for convergence
    #: and for a page that is never laid down square.
    stereo_margin: float = 0.012
    orientation: Orientation = "auto"
    coc_px: float = DEFAULT_COC_PX
    # Sub-pixel disparity accuracy assumed for the stereo estimate.
    stereo_disparity_px: float = 0.2

    def __post_init__(self) -> None:
        if self.n_cameras < 1:
            raise ValueError("n_cameras must be >= 1")
        if self.overlap_mm < 0:
            raise ValueError("overlap_mm must be >= 0")
        if self.coverage == "stereo":
            if self.n_cameras < 2:
                raise ValueError("stereo coverage needs at least 2 cameras")
            if self.stereo_baseline_mm <= 0:
                raise ValueError("stereo_baseline_mm must be > 0")
        elif self.n_cameras > 1 and self.tile_width_mm >= self.document.long_mm:
            raise ValueError("overlap too large: tiles would not reduce the field")

    # -- tiling ------------------------------------------------------------

    @property
    def is_stereo(self) -> bool:
        return self.coverage == "stereo" and self.n_cameras >= 2

    @property
    def tile_width_mm(self) -> float:
        """
        Width of one camera's share of the document along the split axis.

        Tiling: n tiles of width w overlapping by `overlap` at each of the
        n-1 internal joins must span the document, so
            n*w - (n-1)*overlap = L.

        Stereo: every camera covers the whole thing, plus a margin.
        """
        if self.coverage == "stereo":
            return self.document.long_mm * (1.0 + self.stereo_margin)
        n = self.n_cameras
        return (self.document.long_mm + (n - 1) * self.overlap_mm) / n

    @property
    def tiles(self) -> list[Tile]:
        if self.coverage == "stereo":
            return [Tile(i, 0.0, self.document.long_mm) for i in range(self.n_cameras)]
        w = self.tile_width_mm
        step = w - self.overlap_mm if self.n_cameras > 1 else 0.0
        return [Tile(i, i * step, i * step + w) for i in range(self.n_cameras)]

    @property
    def baseline_mm(self) -> float:
        """
        Separation between adjacent camera optical axes.

        When tiling this is *forced* by the tile spacing -- widening the
        overlap to gain stereo coverage drags the cameras together and
        destroys the depth precision you were trying to buy.  In stereo
        coverage it is yours to choose.
        """
        if self.n_cameras < 2:
            return 0.0
        if self.coverage == "stereo":
            return self.stereo_baseline_mm
        return self.tiles[1].centre_mm - self.tiles[0].centre_mm

    @property
    def stereo_coverage_fraction(self) -> float:
        """Fraction of the document seen by at least two cameras."""
        if self.n_cameras < 2:
            return 0.0
        if self.coverage == "stereo":
            return 1.0
        return min(1.0, self.overlap_mm / self.document.long_mm)

    @property
    def convergence_deg(self) -> float:
        """Angle between the two cameras' optical axes, degrees."""
        if self.n_cameras < 2 or self.baseline_mm == 0:
            return 0.0
        return 2.0 * math.degrees(
            math.atan(self.baseline_mm / 2.0 / self.working_distance_mm)
        )

    # -- orientation solve -------------------------------------------------

    def _solve(self, orientation: str) -> tuple[float, str]:
        """Return (px_per_mm, binding_axis) for a sensor orientation.

        'portrait'  -> the short sensor axis (fewer pixels) spans the tile width
        'landscape' -> the long sensor axis spans the tile width
        """
        s = self.sensor
        if orientation == "portrait":
            across_px, along_px = s.short_px, s.long_px
            across_axis, along_axis = "short", "long"
        else:
            across_px, along_px = s.long_px, s.short_px
            across_axis, along_axis = "long", "short"

        ppm_across = across_px / self.tile_width_mm
        ppm_along = along_px / self.document.short_mm
        if ppm_across <= ppm_along:
            return ppm_across, across_axis
        return ppm_along, along_axis

    @property
    def resolved_orientation(self) -> str:
        if self.orientation != "auto":
            return self.orientation
        p, _ = self._solve("portrait")
        l, _ = self._solve("landscape")
        return "portrait" if p >= l else "landscape"

    @property
    def binding_axis(self) -> str:
        """Which sensor axis limits resolution -- 'long' or 'short'."""
        return self._solve(self.resolved_orientation)[1]

    # -- resolution --------------------------------------------------------

    @property
    def px_per_mm(self) -> float:
        return self._solve(self.resolved_orientation)[0]

    @property
    def dpi(self) -> float:
        return self.px_per_mm * 25.4

    @property
    def spread_px(self) -> tuple[int, int]:
        """Stitched output size for the full document, (long, short)."""
        return (
            round(self.document.long_mm * self.px_per_mm),
            round(self.document.short_mm * self.px_per_mm),
        )

    @property
    def spread_megapixels(self) -> float:
        w, h = self.spread_px
        return w * h / 1e6

    @property
    def tile_px(self) -> tuple[int, int]:
        """Useful pixels per tile, (across split axis, along short axis)."""
        return (
            round(self.tile_width_mm * self.px_per_mm),
            round(self.document.short_mm * self.px_per_mm),
        )

    # -- imaging geometry --------------------------------------------------

    @property
    def _binding_sensor_mm(self) -> float:
        s = self.sensor
        return s.long_mm if self.binding_axis == "long" else s.short_mm

    @property
    def _binding_pitch_mm(self) -> float:
        s = self.sensor
        return s.pitch_long_mm if self.binding_axis == "long" else s.pitch_short_mm

    @property
    def magnification(self) -> float:
        """Sensor mm per object mm."""
        return self._binding_pitch_mm * self.px_per_mm

    @property
    def working_distance_mm(self) -> float:
        """Object to lens principal plane: f*(1 + 1/m)."""
        return self.lens.focal_mm * (1.0 + 1.0 / self.magnification)

    @property
    def focal_px(self) -> float:
        """Focal length in pixels -- the f of the pinhole camera matrix."""
        return self.lens.focal_mm / self._binding_pitch_mm

    @property
    def field_along_mm(self) -> float:
        """Coverage on the non-binding axis -- how much page it sees."""
        s = self.sensor
        other_mm = s.short_mm if self.binding_axis == "long" else s.long_mm
        return other_mm / self.magnification

    @property
    def spare_field_mm(self) -> float:
        """
        Unused field on the non-binding axis, total.

        This is not waste to be optimised away: it is the margin that lets
        deskew and crop work on a page that is never perfectly square.
        """
        return max(0.0, self.field_along_mm - self.document.short_mm)

    # -- focus and diffraction --------------------------------------------

    @property
    def coc_mm(self) -> float:
        return self.coc_px * self._binding_pitch_mm

    @property
    def depth_of_field_mm(self) -> float:
        """Total DOF at close focus: 2*N*c*(1+m)/m^2."""
        m = self.magnification
        return 2.0 * self.lens.f_number * self.coc_mm * (1.0 + m) / (m * m)

    @property
    def airy_diameter_mm(self) -> float:
        return 2.44 * LAMBDA_MM * self.lens.f_number

    @property
    def airy_diameter_px(self) -> float:
        return self.airy_diameter_mm / self._binding_pitch_mm

    @property
    def diffraction_limited(self) -> bool:
        """True once the Airy disc exceeds two pixels -- stop down no further."""
        return self.airy_diameter_px > 2.0

    # -- stereo over the overlap ------------------------------------------

    @property
    def stereo_dz_um(self) -> float:
        """
        Height precision in the overlap band, in micrometres.

            dz = z^2 * du / (f_px * b)

        Free, because the overlap exists anyway, and it lands exactly over
        the gutter where page curvature is worst.
        """
        if self.n_cameras < 2:
            return float("inf")
        z = self.working_distance_mm
        return (
            z * z * self.stereo_disparity_px / (self.focal_px * self.baseline_mm)
        ) * 1000.0

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict:
        w, h = self.spread_px
        return {
            "cameras": self.n_cameras,
            "document": self.document.name,
            "orientation": self.resolved_orientation,
            "binding_axis": self.binding_axis,
            "coverage": self.coverage,
            "overlap_mm": round(self.overlap_mm, 2),
            "stereo_coverage_pct": round(100 * self.stereo_coverage_fraction, 1),
            "convergence_deg": round(self.convergence_deg, 1),
            "tile_width_mm": round(self.tile_width_mm, 2),
            "baseline_mm": round(self.baseline_mm, 2),
            "dpi": round(self.dpi, 1),
            "px_per_mm": round(self.px_per_mm, 4),
            "spread_px": [w, h],
            "spread_mp": round(self.spread_megapixels, 2),
            "magnification": round(self.magnification, 5),
            "working_distance_mm": round(self.working_distance_mm, 1),
            "focal_px": round(self.focal_px, 1),
            "field_along_mm": round(self.field_along_mm, 1),
            "spare_field_mm": round(self.spare_field_mm, 1),
            "dof_mm": round(self.depth_of_field_mm, 2),
            "airy_px": round(self.airy_diameter_px, 2),
            "diffraction_limited": self.diffraction_limited,
            "stereo_dz_um": (
                round(self.stereo_dz_um, 1)
                if math.isfinite(self.stereo_dz_um)
                else None
            ),
        }

    def report(self) -> str:
        s = self.summary()
        lines = [
            f"{s['cameras']} x {self.sensor.name} @ {self.lens.focal_mm:g} mm "
            f"f/{self.lens.f_number:g}  ->  {self.document.name}",
            "-" * 66,
            f"  orientation        {s['orientation']}  "
            f"(limited by the {s['binding_axis']} sensor axis)",
            f"  overlap            {s['overlap_mm']:g} mm",
            f"  tile width         {s['tile_width_mm']:g} mm",
            f"  baseline           {s['baseline_mm']:g} mm",
            "",
            f"  RESOLUTION         {s['dpi']:g} DPI   "
            f"({s['spread_px'][0]} x {s['spread_px'][1]} px, {s['spread_mp']:g} MP)",
            "",
            f"  magnification      {s['magnification']:.5f}",
            f"  working distance   {s['working_distance_mm']:g} mm",
            f"  field (other axis) {s['field_along_mm']:g} mm "
            f"({s['spare_field_mm']:g} mm spare)",
            f"  depth of field     {s['dof_mm']:g} mm "
            f"(+/-{s['dof_mm'] / 2:.1f} mm)",
            f"  Airy disc          {s['airy_px']:g} px"
            + ("   <-- diffraction limited" if s["diffraction_limited"] else ""),
        ]
        if s["stereo_dz_um"] is not None:
            lines.append(
                f"  stereo dz          {s['stereo_dz_um']:g} um "
                f"over {s['stereo_coverage_pct']:g} % of the page"
                + (f", convergence {s['convergence_deg']:g} deg" if self.is_stereo else "")
            )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Named configurations
# --------------------------------------------------------------------------

#: Blueprint v1.0 decision of record.
BLUEPRINT_V1 = RigGeometry(n_cameras=2, overlap_mm=20.0, document=A3)

#: What you can do tomorrow with the single body that arrives.
SINGLE_CAMERA_A3 = RigGeometry(n_cameras=1, document=A3)

#: Single body on A4 -- comfortably past the CZUR, today, with one camera.
SINGLE_CAMERA_A4 = RigGeometry(n_cameras=1, document=A4)

#: Full-overlap stereo: both bodies see the whole spread, so the page
#: surface can be recovered and flattened.  Costs resolution, buys 3-D.
STEREO_A3 = RigGeometry(n_cameras=2, coverage="stereo", stereo_baseline_mm=200.0,
                        document=A3)


def compare(configs: dict[str, RigGeometry]) -> str:
    """Small table comparing several configurations."""
    head = (
        f"{'config':<26}{'DPI':>8}{'MP':>8}{'tile':>9}"
        f"{'WD':>9}{'DOF':>8}{'spare':>8}"
    )
    rows = [head, "-" * len(head)]
    for name, g in configs.items():
        rows.append(
            f"{name:<26}{g.dpi:>8.0f}{g.spread_megapixels:>8.1f}"
            f"{g.tile_width_mm:>9.0f}{g.working_distance_mm:>9.0f}"
            f"{g.depth_of_field_mm:>8.1f}{g.spare_field_mm:>8.0f}"
        )
    return "\n".join(rows)


if __name__ == "__main__":  # pragma: no cover
    print(BLUEPRINT_V1.report())
    print()
    print(
        compare(
            {
                "blueprint v1.0 (2 cam)": BLUEPRINT_V1,
                "1 camera, A3": SINGLE_CAMERA_A3,
                "1 camera, A4": SINGLE_CAMERA_A4,
            }
        )
    )
