"""
Command line interface.

    python -m scanner geometry            what the optics can do
    python -m scanner board -o board.png  printable ChArUco target
    python -m scanner selftest            rehearse the whole rig, no hardware
    python -m scanner node                run a camera node
    python -m scanner capture             drive a session
    python -m scanner process             raw frames -> masters
    python -m scanner measure             MTF50 / DPI / dE on an image
    python -m scanner gui                 the operator interface in a browser
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _print(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("-" * max(20, len(title)))


# --------------------------------------------------------------------------


def cmd_geometry(args) -> int:
    from .geometry import (A3, A4, FORMATS, RigGeometry, Lens, compare,
                           SINGLE_CAMERA_A3, SINGLE_CAMERA_A4, BLUEPRINT_V1)

    doc = FORMATS.get(args.format, A3)
    g = RigGeometry(
        n_cameras=args.cameras,
        overlap_mm=args.overlap,
        document=doc,
        lens=Lens(args.focal, args.aperture),
    )
    print(g.report())

    if args.compare:
        _print("alternatives")
        print(compare({
            "blueprint v1.0 (2 cam)": BLUEPRINT_V1,
            "1 camera, A3": SINGLE_CAMERA_A3,
            "1 camera, A4": SINGLE_CAMERA_A4,
            "this config": g,
        }))
        print("\nCZUR ET Ultra, for reference:      441 DPI    39.8 MP")
    if args.json:
        print(json.dumps(g.summary(), indent=2))
    return 0


def cmd_board(args) -> int:
    import cv2

    from .calib.intrinsics import BoardSpec

    spec = BoardSpec(squares_x=args.squares_x, squares_y=args.squares_y,
                     square_mm=args.square_mm)
    img = spec.render(px_per_mm=args.dpi / 25.4, margin_mm=args.margin)
    out = Path(args.out)
    cv2.imwrite(str(out), img)
    w, h = spec.rendered_extent_mm(args.margin)
    print(f"wrote {out}  ({img.shape[1]} x {img.shape[0]} px)")
    print(f"  pattern   {spec.squares_x} x {spec.squares_y} squares @ "
          f"{spec.square_mm:g} mm  =  {spec.size_mm[0]:g} x {spec.size_mm[1]:g} mm")
    print(f"  print at  {args.dpi:g} DPI, page size {w:g} x {h:g} mm")
    print("\nPrint it at 100 % scale -- 'fit to page' will silently rescale it")
    print("and put that error straight into every calibration. Then measure a")
    print("square with a ruler and confirm it really is "
          f"{spec.square_mm:g} mm before you shoot anything.")
    print("Mount it on something rigid and flat. A curled sheet is a curved lens.")
    return 0


def cmd_node(args) -> int:
    import os

    import uvicorn

    if args.camera_id:
        os.environ["SCANNER_CAMERA_ID"] = args.camera_id
    if args.backend:
        os.environ["SCANNER_BACKEND"] = args.backend
    os.environ.setdefault("SCANNER_TILE", str(args.tile))
    print(f"node starting: camera={os.environ.get('SCANNER_CAMERA_ID', 'auto')} "
          f"backend={os.environ.get('SCANNER_BACKEND', 'auto')} "
          f"tile={os.environ['SCANNER_TILE']}")
    uvicorn.run("scanner.node.server:app", host=args.host, port=args.port,
                log_level=args.log_level)
    return 0


def cmd_capture(args) -> int:
    from .orchestrator.session import CaptureSession, NodeSpec

    specs = []
    for i, url in enumerate(args.node):
        cid, _, u = url.partition("=")
        if not u:
            cid, u = f"cam{i}", url
        specs.append(NodeSpec(cid, u, i))

    session = CaptureSession(specs, args.out, profile=args.profile)
    _print("nodes")
    for cid, st in session.check().items():
        if st.get("error"):
            print(f"  {cid:<8} UNREACHABLE  {st['error']}")
        else:
            print(f"  {cid:<8} {st.get('model', '?'):<28} "
                  f"{'connected' if st.get('connected') else 'NO CAMERA'}")
    if not args.force:
        session.require_ready()

    if args.iso or args.shutter or args.aperture:
        session.configure_all(iso=args.iso, shutter=args.shutter,
                              aperture=args.aperture)

    _print(f"capturing {args.count} spread(s), profile={args.profile}")
    for i in range(args.count):
        rec = session.capture_spread()
        flag = "ok " if rec.complete else "ERR"
        print(f"  [{i + 1}/{args.count}] {flag} seq={rec.seq} "
              f"{rec.elapsed_s:5.2f}s  {rec.bytes_total / 1e6:6.1f} MB"
              + (f"  {rec.errors}" if rec.errors else ""))
        if args.interactive and i < args.count - 1:
            input("      turn the page, press enter... ")

    _print("throughput")
    for k, v in session.throughput().items():
        print(f"  {k:<12} {v}")
    print(f"\nmanifest: {session.write_manifest()}")
    return 0


def cmd_process(args) -> int:
    import cv2

    from .calib.store import RigCalibration
    from .pipeline.run import PipelineConfig, process_spread

    rig = RigCalibration.load(args.calibration)
    cfg = PipelineConfig(
        deskew=not args.no_deskew,
        normalise_background=not args.no_normalise,
        split_spread=not args.no_split,
        debug_dir=args.debug_dir,
    )
    root = Path(args.input)
    spread_dirs = sorted(d for d in root.iterdir() if d.is_dir()) if root.is_dir() else []
    if not spread_dirs:
        spread_dirs = [root]

    out = Path(args.out)
    total = 0
    for d in spread_dirs:
        frames = {}
        for cid in rig.cameras:
            hits = sorted(d.glob(f"{cid}_*"))
            if hits:
                frames[cid] = cv2.imread(str(hits[0]), cv2.IMREAD_COLOR)
        if not frames:
            print(f"  {d.name}: no frames matching any camera id, skipped")
            continue
        res = process_spread(frames, rig, cfg)
        paths = res.save(out, d.name)
        total += 1
        print(f"  {d.name}: {res.report['megapixels']} MP  "
              f"{res.report['total_ms']:.0f} ms -> {Path(paths['master']).name}")
    print(f"\n{total} spread(s) -> {out}")
    return 0


def cmd_measure(args) -> int:
    import cv2

    from .metrics import measure_edges, measure_scale

    img = cv2.imread(args.image, cv2.IMREAD_COLOR)
    if img is None:
        print(f"cannot read {args.image}", file=sys.stderr)
        return 2
    print(f"{args.image}: {img.shape[1]} x {img.shape[0]} px")

    if args.edge:
        rois = [tuple(int(v) for v in e.split(",")) for e in args.edge]
        _print("sharpness (ISO 12233 slanted edge)")
        for r, res in zip(rois, measure_edges(img, rois)):
            line = (f"  roi {r}  MTF50 {res.mtf50:.4f} cy/px   "
                    f"MTF20 {res.mtf20:.4f}   edge {res.angle_deg:+.2f} deg")
            if args.px_per_mm:
                line += f"   {res.lp_per_mm(args.px_per_mm):.1f} lp/mm"
            print(line + ("   PASS" if res.mtf50 >= 0.30 else "   below 0.30 target"))

    if args.scale:
        ax, ay, bx, by, span = (float(v) for v in args.scale.split(","))
        res = measure_scale(img, (ax, ay), (bx, by), span)
        _print("scale")
        print(f"  {res.px_per_mm:.4f} px/mm = {res.dpi:.1f} DPI "
              f"({res.distance_px:.2f} px over {span:g} mm)")
    return 0


def cmd_selftest(args) -> int:
    """Rehearse the entire rig against synthetic captures.  No hardware."""
    from .selftest import run_selftest

    return run_selftest(
        cameras=args.cameras, overlap=args.overlap, scale=args.scale,
        out_dir=args.out, verbose=not args.quiet,
    )


def cmd_gui(args) -> int:
    """Serve the operator interface."""
    import uvicorn

    from .gui.server import GuiState, create_gui, parse_node, start_mock_nodes

    specs = list(args.node or [])
    if args.mock:
        print(f"starting {args.mock_cameras} mock camera nodes ...")
        specs = start_mock_nodes(args.mock_cameras, scale=args.mock_scale)
        for s in specs:
            print(f"  {s}")

    state = GuiState(
        nodes=[parse_node(s, i) for i, s in enumerate(specs)],
        out_dir=args.out, profile=args.profile, coverage=args.coverage,
        overlap_mm=args.overlap, baseline_mm=args.baseline,
        document=args.format, focal_mm=args.focal, f_number=args.aperture,
    )
    shown = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    print(f"\n  operator interface:  http://{shown}:{args.port}\n")
    uvicorn.run(create_gui(state), host=args.host, port=args.port,
                log_level=args.log_level)
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scanner",
        description="Stereoscopic overhead document scanner -- Blueprint v1.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("geometry", help="what the optics can do")
    g.add_argument("-n", "--cameras", type=int, default=2)
    g.add_argument("-o", "--overlap", type=float, default=20.0)
    g.add_argument("-f", "--format", default="A3 spread")
    g.add_argument("--focal", type=float, default=30.0)
    g.add_argument("--aperture", type=float, default=8.0)
    g.add_argument("--compare", action="store_true")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_geometry)

    b = sub.add_parser("board", help="render a printable ChArUco target")
    b.add_argument("-o", "--out", default="charuco_a4.png")
    b.add_argument("--squares-x", type=int, default=7)
    b.add_argument("--squares-y", type=int, default=10)
    b.add_argument("--square-mm", type=float, default=26.0)
    b.add_argument("--margin", type=float, default=6.0)
    b.add_argument("--dpi", type=float, default=600.0)
    b.set_defaults(func=cmd_board)

    n = sub.add_parser("node", help="run a camera node")
    n.add_argument("--host", default="0.0.0.0")
    n.add_argument("--port", type=int, default=8000)
    n.add_argument("--camera-id")
    n.add_argument("--tile", type=int, default=0)
    n.add_argument("--backend", choices=["mock", "gphoto2", "auto"])
    n.add_argument("--log-level", default="info")
    n.set_defaults(func=cmd_node)

    c = sub.add_parser("capture", help="drive a capture session")
    c.add_argument("--node", action="append", required=True,
                   metavar="[cam0=]http://host:8000")
    c.add_argument("-o", "--out", default="captures")
    c.add_argument("-c", "--count", type=int, default=1)
    c.add_argument("-p", "--profile", default="standard",
                   choices=["standard", "clean", "max"])
    c.add_argument("--iso", type=int)
    c.add_argument("--shutter")
    c.add_argument("--aperture")
    c.add_argument("--interactive", action="store_true",
                   help="pause between spreads to turn the page")
    c.add_argument("--force", action="store_true",
                   help="capture even if a node reports it is not ready")
    c.set_defaults(func=cmd_capture)

    pr = sub.add_parser("process", help="raw frames -> archival masters")
    pr.add_argument("input")
    pr.add_argument("-k", "--calibration", required=True)
    pr.add_argument("-o", "--out", default="masters")
    pr.add_argument("--no-deskew", action="store_true")
    pr.add_argument("--no-normalise", action="store_true")
    pr.add_argument("--no-split", action="store_true")
    pr.add_argument("--debug-dir")
    pr.set_defaults(func=cmd_process)

    m = sub.add_parser("measure", help="MTF50 / DPI on an image")
    m.add_argument("image")
    m.add_argument("--edge", action="append", metavar="x,y,w,h")
    m.add_argument("--scale", metavar="ax,ay,bx,by,span_mm")
    m.add_argument("--px-per-mm", type=float)
    m.set_defaults(func=cmd_measure)

    s = sub.add_parser("selftest", help="rehearse the whole rig, no hardware")
    s.add_argument("-n", "--cameras", type=int, default=2)
    s.add_argument("-o", "--overlap", type=float, default=20.0)
    s.add_argument("--scale", type=float, default=0.25,
                   help="render scale; 1.0 is full 24 MP and slow")
    s.add_argument("--out", default="selftest_out")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(func=cmd_selftest)

    ui = sub.add_parser("gui", help="operator interface in a browser")
    ui.add_argument("--node", action="append", metavar="[cam0=]http://host:8000")
    ui.add_argument("--mock", action="store_true",
                    help="spin up simulated cameras in this process")
    ui.add_argument("--mock-cameras", type=int, default=2)
    ui.add_argument("--mock-scale", type=float, default=0.12,
                    help="render scale for the mock cameras; 1.0 is 24 MP")
    ui.add_argument("--host", default="127.0.0.1",
                    help="0.0.0.0 to reach it from a phone on the same LAN")
    ui.add_argument("--port", type=int, default=8800)
    ui.add_argument("-o", "--out", default="captures")
    ui.add_argument("-p", "--profile", default="standard",
                    choices=["standard", "clean", "max"])
    ui.add_argument("--coverage", default="stereo", choices=["stereo", "tile"])
    ui.add_argument("-f", "--format", default="A3 spread")
    ui.add_argument("--overlap", type=float, default=20.0)
    ui.add_argument("--baseline", type=float, default=200.0)
    ui.add_argument("--focal", type=float, default=30.0)
    ui.add_argument("--aperture", type=float, default=8.0)
    ui.add_argument("--log-level", default="warning")
    ui.set_defaults(func=cmd_gui)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
