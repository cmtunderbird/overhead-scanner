"""
Capture session: the laptop's side of the conversation.

Two rules make this robust, and both are deliberate:

1. **Pairing is by a sequence counter issued here, never by timestamp.**
   Nothing in the scene moves, so the frames do not need to be
   simultaneous to a millisecond -- they need to be *identifiable*.  A
   counter is exact and immune to clock skew, NTP steps and network
   jitter.  Timestamp pairing works in the lab and fails on page 300.

2. **A node failure loses one spread, not the session.**  Each capture is
   independent; the manifest records what succeeded and what did not, and
   a partial spread is written as partial rather than silently dropped.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
import requests


@dataclass
class NodeSpec:
    camera_id: str
    url: str
    tile_index: int = 0

    @property
    def base(self) -> str:
        return self.url.rstrip("/")


@dataclass
class CaptureRecord:
    seq: int
    ok: bool
    profile: str
    elapsed_s: float
    files: dict[str, list[str]] = field(default_factory=dict)
    bytes_total: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return self.ok and not self.errors


class NodeClient:
    """Thin HTTP client for one node."""

    def __init__(self, spec: NodeSpec, timeout: float = 30.0):
        self.spec = spec
        self.timeout = timeout
        self.session = requests.Session()

    def status(self) -> dict:
        r = self.session.get(f"{self.spec.base}/status", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def configure(self, **kwargs) -> dict:
        r = self.session.post(
            f"{self.spec.base}/config", json=kwargs, timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def connect(self) -> dict:
        r = self.session.post(f"{self.spec.base}/connect", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def capture(self, seq: int, profile: str = "standard") -> dict:
        r = self.session.post(
            f"{self.spec.base}/capture",
            json={"seq": seq, "profile": profile},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def fetch(self, file_id: str) -> bytes:
        r = self.session.get(
            f"{self.spec.base}/files/{file_id}", timeout=self.timeout
        )
        r.raise_for_status()
        return r.content

    def preview(self) -> bytes:
        r = self.session.get(f"{self.spec.base}/preview", timeout=self.timeout)
        r.raise_for_status()
        return r.content


class CaptureSession:
    """
    Drives N nodes through a book.

    Works identically with one node or two.  That is the point: nothing
    here changes when the second body arrives, so the code you write and
    test today is the code that ships.
    """

    def __init__(
        self,
        nodes: list[NodeSpec],
        out_dir: str | Path,
        *,
        profile: str = "standard",
        start_seq: int = 0,
        timeout: float = 30.0,
    ):
        if not nodes:
            raise ValueError("a session needs at least one node")
        self.nodes = nodes
        self.clients = {n.camera_id: NodeClient(n, timeout) for n in nodes}
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.profile = profile
        self.seq = start_seq
        self.records: list[CaptureRecord] = []

    # -- setup -------------------------------------------------------------

    def check(self) -> dict[str, dict]:
        """Ask every node how it is.  Call this before a long run."""
        out = {}
        for cid, c in self.clients.items():
            try:
                out[cid] = c.status()
            except Exception as e:  # noqa: BLE001
                out[cid] = {"error": str(e), "connected": False}
        return out

    def require_ready(self) -> None:
        """Fail loudly, now, rather than on page 200."""
        problems = []
        for cid, st in self.check().items():
            if st.get("error"):
                problems.append(f"{cid}: unreachable ({st['error']})")
            elif not st.get("connected"):
                problems.append(f"{cid}: node up but camera not connected")
        if problems:
            raise RuntimeError("nodes not ready:\n  " + "\n  ".join(problems))

    def configure_all(self, **kwargs) -> dict[str, dict]:
        return {cid: c.configure(**kwargs) for cid, c in self.clients.items()}

    # -- capture -----------------------------------------------------------

    def capture_spread(self, profile: str | None = None) -> CaptureRecord:
        """
        Fire every camera on one sequence number and pull the files down.

        Cameras are triggered concurrently.  They are not synchronised to
        the microsecond and do not need to be.
        """
        prof = profile or self.profile
        seq = self.seq
        self.seq += 1
        t0 = time.perf_counter()

        rec = CaptureRecord(seq=seq, ok=True, profile=prof, elapsed_s=0.0)
        spread_dir = self.out_dir / f"{seq:05d}"

        def one(cid: str) -> tuple[str, list[str], int, str | None]:
            client = self.clients[cid]
            try:
                resp = client.capture(seq, prof)
            except Exception as e:  # noqa: BLE001
                return cid, [], 0, f"capture: {e}"
            paths, total = [], 0
            spread_dir.mkdir(parents=True, exist_ok=True)
            for f in resp["files"]:
                try:
                    data = client.fetch(f["file_id"])
                except Exception as e:  # noqa: BLE001
                    return cid, paths, total, f"fetch {f['file_id']}: {e}"
                ext = ".png" if f.get("content_type") == "image/png" else ".arw"
                p = spread_dir / f"{cid}_{f['frame_index']}{ext}"
                p.write_bytes(data)
                paths.append(str(p))
                total += len(data)
            return cid, paths, total, None

        with ThreadPoolExecutor(max_workers=len(self.clients)) as pool:
            for cid, paths, total, err in pool.map(one, list(self.clients)):
                rec.files[cid] = paths
                rec.bytes_total += total
                if err:
                    rec.errors[cid] = err
                    rec.ok = False

        rec.elapsed_s = round(time.perf_counter() - t0, 3)
        self.records.append(rec)
        return rec

    def capture_many(self, n: int, on_spread=None) -> list[CaptureRecord]:
        out = []
        for _ in range(n):
            rec = self.capture_spread()
            out.append(rec)
            if on_spread:
                on_spread(rec)
        return out

    # -- reporting ---------------------------------------------------------

    def throughput(self) -> dict:
        """
        Was the run actually sustainable?

        The inequality that matters: transfer time per spread must stay
        below page-turn time, or the queue grows without bound.
        """
        if not self.records:
            return {}
        times = np.array([r.elapsed_s for r in self.records])
        mb = np.array([r.bytes_total for r in self.records]) / 1e6
        return {
            "spreads": len(self.records),
            "complete": sum(r.complete for r in self.records),
            "failed": sum(not r.ok for r in self.records),
            "mean_s": round(float(times.mean()), 3),
            "p95_s": round(float(np.percentile(times, 95)), 3),
            "max_s": round(float(times.max()), 3),
            "mean_mb": round(float(mb.mean()), 2),
            "mb_per_s": round(float(mb.sum() / max(times.sum(), 1e-6)), 2),
        }

    def write_manifest(self, name: str = "session.json") -> Path:
        path = self.out_dir / name
        path.write_text(
            json.dumps(
                {
                    "nodes": [asdict(n) for n in self.nodes],
                    "profile": self.profile,
                    "throughput": self.throughput(),
                    "captures": [asdict(r) for r in self.records],
                },
                indent=2,
            )
        )
        return path
