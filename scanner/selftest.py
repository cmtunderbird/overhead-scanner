"""
Full-rig rehearsal against synthetic captures.

Runs the entire chain -- calibrate, align, flat-field, colour, capture,
stitch, split, measure -- and prints the blueprint's acceptance table.
No cameras, no rig, no lamps.

The point is that when the hardware arrives, none of this is new code.
You swap the mock backend for gphoto2 and run the same commands.
"""

from __future__ import annotations

import time
from pathlib import Path

import cv2
import numpy as np

from .calib.align import BoardPlacement, mounting_error, solve_document_homography
from .calib.field import apply_colour_matrix, apply_flat_field, calibrate_colour, calibrate_flat_field
from .calib.intrinsics import A4_BOARD, calibrate_intrinsics
from .calib.store import RigCalibration
from .geometry import A3, RigGeometry
from .metrics import chart_delta_e, cross_camera_delta_e, measure_edges, measure_scale
from .pipeline.run import PipelineConfig, process_spread
from .synth.camera import (BODY_A, BODY_B, default_calibration_poses,
                           simulate_board_capture, simulate_flat_field, simulate_rig)
from .synth.page import COLORCHECKER_SRGB, render_page

BODIES = [BODY_A, BODY_B]
GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _row(name: str, value, target: str, ok: bool | None) -> str:
    mark = "  --  " if ok is None else (f"{GREEN} PASS {RESET}" if ok else f"{RED} FAIL {RESET}")
    v = f"{value:.3f}" if isinstance(value, float) else str(value)
    return f"  {name:<34}{v:>12}   {target:<20}{mark}"


def run_selftest(
    cameras: int = 2,
    overlap: float = 20.0,
    scale: float = 0.25,
    out_dir: str | Path = "selftest_out",
    verbose: bool = True,
) -> int:
    t_start = time.perf_counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    geom = RigGeometry(n_cameras=cameras, overlap_mm=overlap, document=A3)
    bodies = [BODIES[i % len(BODIES)] for i in range(cameras)]
    ids = [f"cam{i}" for i in range(cameras)]

    if verbose:
        print(geom.report())
        print(f"\nrendering at scale {scale:g} "
              f"({int(geom.sensor.short_px * scale)} x "
              f"{int(geom.sensor.long_px * scale)} px per frame)")

    # The synthetic page must be rendered well above the capture sampling,
    # or the *page* band-limits the result and the MTF50 measurement is
    # reporting the renderer rather than the optics.  3x is comfortable.
    ppm_out = geom.px_per_mm * scale
    page_ppm = max(6.0, 3.2 * ppm_out)
    board_ppm = max(8.0, 2.0 * ppm_out)
    sampling_headroom = page_ppm / ppm_out
    board = A4_BOARD.render(px_per_mm=board_ppm)
    rig = RigCalibration(
        doc_long_mm=geom.document.long_mm,
        doc_short_mm=geom.document.short_mm,
        target_px_per_mm=geom.px_per_mm * scale,
        overlap_mm=geom.overlap_mm,
        provenance={"source": "selftest", "scale": scale},
    )
    checks: list[tuple[str, object, str, bool | None]] = []

    # ---- 1. intrinsics ---------------------------------------------------
    if verbose:
        print("\n[1/6] intrinsics from tilted ChArUco views")
    poses = default_calibration_poses(geom, 12, seed=11)
    for i, cid in enumerate(ids):
        views = [
            simulate_board_capture(board, board_ppm, geom, p, bodies[i],
                                   scale=scale, seed=500 + i * 50 + j)[0]
            for j, p in enumerate(poses)
        ]
        calib, diag = calibrate_intrinsics(views, A4_BOARD, cid, min_views=5)
        calib.tile_x0_mm = geom.tiles[i].x0_mm
        calib.tile_x1_mm = geom.tiles[i].x1_mm
        rig.cameras[cid] = calib
        if verbose:
            print(f"      {cid}: rms {diag['rms_px']:.3f} px, "
                  f"{diag['views_used']}/{len(views)} views, "
                  f"tilt {diag['tilt_deg_min']}-{diag['tilt_deg_max']} deg")
        checks.append((f"{cid} reprojection rms", float(diag["rms_px"]),
                       "< 0.5 px", diag["rms_px"] < 0.5))
        f_err = abs(calib.K[0, 0] / (geom.focal_px * scale) - 1.0) * 100
        checks.append((f"{cid} focal length error", f_err, "< 2 %", f_err < 2.0))

    # ---- 2. document alignment -------------------------------------------
    if verbose:
        print("[2/6] document alignment")
    margin = 6.0
    ew, eh = A4_BOARD.rendered_extent_mm(margin)
    for i, cid in enumerate(ids):
        img_origin = (geom.tiles[i].centre_mm - ew / 2,
                      geom.document.short_mm / 2 - eh / 2)
        canvas = np.full((int(geom.document.short_mm * board_ppm),
                          int(geom.document.long_mm * board_ppm), 3), 235, np.uint8)
        bimg = cv2.resize(board, (int(ew * board_ppm), int(eh * board_ppm)))
        x0, y0 = int(img_origin[0] * board_ppm), int(img_origin[1] * board_ppm)
        canvas[y0:y0 + bimg.shape[0], x0:x0 + bimg.shape[1]] = bimg
        shot = simulate_rig(canvas, board_ppm, geom, bodies, scale=scale, seed=900)[i][0]
        place = BoardPlacement(origin_mm=(img_origin[0] + margin, img_origin[1] + margin))
        a = solve_document_homography(shot, rig.cameras[cid], place, A4_BOARD)
        me = mounting_error(rig.cameras[cid], geom.document.short_mm)
        if verbose:
            print(f"      {cid}: rms {a.rms_px:.3f} px | mount "
                  f"dx {me['dx_mm']:+.2f} dy {me['dy_mm']:+.2f} mm "
                  f"rot {me['rotation_deg']:+.3f} deg scale {me['scale_error_pct']:+.3f} %")
        checks.append((f"{cid} alignment rms", float(a.rms_px), "< 1.0 px", a.rms_px < 1.0))
        rot_err = abs(me["rotation_deg"] - bodies[i].rotation_deg)
        checks.append((f"{cid} mounting rotation error", rot_err,
                       "< 0.05 deg", rot_err < 0.05))

    # ---- 3. flat-field + colour ------------------------------------------
    if verbose:
        print("[3/6] flat-field and colour")
    page, truth = render_page(geom.document.long_mm, geom.document.short_mm,
                              px_per_mm=page_ppm, spread=True, seed=3)
    caps = simulate_rig(page, page_ppm, geom, bodies, scale=scale, seed=42)
    chart_rois: dict[str, list] = {}
    chart_refs: dict[str, list] = {}

    for i, cid in enumerate(ids):
        c = rig.cameras[cid]
        calibrate_flat_field(
            [simulate_flat_field(geom, bodies[i], scale=scale, seed=770 + k)
             for k in range(3)], c)
        img, ct = caps[i]
        rois, refs = [], []
        for k, p in enumerate(truth.patches):
            if not (ct.tile_x0_mm + 10 < p.cx_mm < ct.tile_x1_mm - 10):
                continue
            u = cv2.perspectiveTransform(
                np.array([[[p.cx_mm, p.cy_mm]]]), ct.H_doc_to_img)[0, 0]
            s = max(6, int(p.size_mm * ct.px_per_mm))
            rois.append((int(u[0] - s / 2), int(u[1] - s / 2), s, s))
            refs.append(COLORCHECKER_SRGB[k % 24])
        chart_rois[cid], chart_refs[cid] = rois, refs
        flat = apply_flat_field(img, c.flat_field)
        before = chart_delta_e(img, rois, refs)
        calibrate_colour(flat, rois, refs, c)
        after = chart_delta_e(apply_colour_matrix(flat, c.colour_matrix), rois, refs)
        if verbose:
            print(f"      {cid}: {len(rois)} patches, "
                  f"dE mean {before['mean']:.2f} -> {after['mean']:.2f}")
        checks.append((f"{cid} chart dE (mean)", float(after["mean"]),
                       "< 2.0", after["mean"] < 2.0))

    if cameras >= 2:
        a_img = apply_colour_matrix(
            apply_flat_field(caps[0][0], rig.cameras[ids[0]].flat_field),
            rig.cameras[ids[0]].colour_matrix)
        b_img = apply_colour_matrix(
            apply_flat_field(caps[1][0], rig.cameras[ids[1]].flat_field),
            rig.cameras[ids[1]].colour_matrix)
        n = min(len(chart_rois[ids[0]]), len(chart_rois[ids[1]]))
        cross = cross_camera_delta_e(a_img, chart_rois[ids[0]][:n],
                                     b_img, chart_rois[ids[1]][:n])
        checks.append(("cross-camera dE (the seam)", float(cross["mean"]),
                       "< 2.0", cross["mean"] < 2.0))
        if verbose:
            print(f"      cross-camera dE mean {cross['mean']:.2f} "
                  f"max {cross['max']:.2f}")

    # ---- 4. pipeline -----------------------------------------------------
    if verbose:
        print("[4/6] capture -> stitch -> split")
    frames = {cid: caps[i][0] for i, cid in enumerate(ids)}
    res = process_spread(frames, rig, PipelineConfig())
    r = res.report
    if verbose:
        print(f"      {r['output_px'][0]} x {r['output_px'][1]} px "
              f"({r['megapixels']} MP) in {r['total_ms']:.0f} ms, "
              f"coverage {r['coverage_pct']} %")
    checks.append(("coverage", float(r["coverage_pct"]), "> 99 %",
                   r["coverage_pct"] > 99.0))
    checks.append(("residual skew", abs(float(r.get("skew_deg", 0.0))),
                   "< 0.3 deg", abs(r.get("skew_deg", 0.0)) < 0.3))
    if r.get("spine"):
        err = abs(r["spine"]["x_mm"] - (truth.spine_mm or 0))
        checks.append(("spine position error", err, "< 5 mm", err < 5.0))

    paths = res.save(out, "selftest")

    # ---- 5. measurements -------------------------------------------------
    if verbose:
        print("[5/6] measuring the result")
    ox, oy = (r["crop_box"][0], r["crop_box"][1]) if "crop_box" in r else (0, 0)
    (ax, ay), (bx, by) = truth.ruler_ends_mm
    try:
        sc = measure_scale(res.master,
                           (ax * ppm_out - ox, ay * ppm_out - oy),
                           (bx * ppm_out - ox, by * ppm_out - oy),
                           span_mm=truth.ruler_span_mm,
                           search_px=int(6 * ppm_out))
        dpi_err = abs(sc.px_per_mm / ppm_out - 1.0) * 100
        checks.append(("measured scale error", dpi_err, "< 1 %", dpi_err < 1.0))
        if verbose:
            print(f"      measured {sc.px_per_mm:.3f} px/mm vs "
                  f"{ppm_out:.3f} predicted ({dpi_err:+.2f} %)")
    except ValueError as e:
        checks.append(("measured scale error", str(e), "< 1 %", False))

    edge_rois = []
    for e in truth.edges:
        x, y, w, h = e.roi_px(ppm_out)
        edge_rois.append((x - ox, y - oy, w, h))
    sfr = measure_edges(res.master, edge_rois)
    if sfr:
        mtf = float(np.median([s.mtf50 for s in sfr]))
        if verbose:
            print(f"      MTF50 median {mtf:.3f} cy/px over {len(sfr)} edges")
        checks.append(("MTF50 (median of edges)", mtf, ">= 0.30 cy/px",
                       mtf >= 0.30 if sampling_headroom >= 3.0 else None))
    else:
        checks.append(("MTF50 (median of edges)", "no edges found",
                       ">= 0.30 cy/px", None))

    # ---- 6. persistence --------------------------------------------------
    cal_path = rig.save(out / "rig_calibration.json")
    back = RigCalibration.load(cal_path)
    ok = all(np.allclose(back.cameras[c].K, rig.cameras[c].K) for c in rig.cameras)
    checks.append(("calibration round-trip", "identical" if ok else "MISMATCH",
                   "identical", ok))

    # ---- report ----------------------------------------------------------
    print(f"\n\033[1macceptance\033[0m  (synthetic rehearsal, scale {scale:g}, "
          f"page rendered at {sampling_headroom:.1f}x the capture sampling)")
    print("  " + "-" * 76)
    for name, value, target, ok in checks:
        print(_row(name, value, target, ok))
    print("  " + "-" * 76)

    failed = [c for c in checks if c[3] is False]
    skipped = [c for c in checks if c[3] is None]
    elapsed = time.perf_counter() - t_start
    print(f"\n  {len(checks) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped   ({elapsed:.1f} s)")
    print(f"  outputs: {out}/")
    for k, v in paths.items():
        print(f"    {k:<8} {Path(v).name}")
    print(f"    {'calib':<8} {cal_path.name}")

    if failed:
        print(f"\n{RED}  These are the numbers that would fail on real hardware "
              f"too.{RESET}")
        return 1
    print(f"\n{GREEN}  Rig rehearsed end to end. Swap the mock backend for "
          f"gphoto2 and none of this changes.{RESET}")
    return 0
