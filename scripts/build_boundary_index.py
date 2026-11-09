#!/usr/bin/env python3
"""Build (or check) the boundary index: one document per page break.

    python scripts/build_boundary_index.py --data-dir build/1.0.0 [--corpus-version 1.0.0]
    python scripts/build_boundary_index.py --data-dir build/1.0.0 --check [--sample 2000]
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def binary() -> Path:
    for name in ("kashshaf-boundary.exe", "kashshaf-boundary"):
        p = ROOT / "indexer/target/release" / name
        if p.exists():
            return p
    sys.exit("kashshaf-boundary not built: cargo build --release --bin kashshaf-boundary, in indexer/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True, help="directory holding corpus.db and metadata.db; boundary_index/ is written beside them")
    ap.add_argument("--corpus-version", default=None, help="stamp for boundary.json (default: corpus.db's)")
    ap.add_argument("--check", action="store_true", help="verify an existing boundary_index/ instead of building")
    ap.add_argument("--sample", type=int, default=2000, help="with --check: documents to verify the midpoint on")
    a = ap.parse_args()

    data_dir: Path = a.data_dir
    out = data_dir / "boundary_index"
    exe = binary()
    if a.check:
        cmd = [str(exe), "--check", "--index", str(out), "--corpus-db", str(data_dir / "corpus.db"), "--sample", str(a.sample)]
        cmd += ["--main-index", str(data_dir / "tantivy_index")]
    else:
        cmd = [str(exe), "--corpus-db", str(data_dir / "corpus.db"), "--metadata-db", str(data_dir / "metadata.db"), "--out", str(out)]
        if a.corpus_version:
            cmd += ["--corpus-version", a.corpus_version]
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, cwd=str(ROOT)).returncode


if __name__ == "__main__":
    sys.exit(main())
