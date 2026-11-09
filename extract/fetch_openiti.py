#!/usr/bin/env python3
"""Fetch OpenITI raw texts by version URI into data/raw/openiti/.

    python extract/fetch_openiti.py --uris 0630IbnCunayn.Diwan.JK007597-ara1
    python extract/fetch_openiti.py --uri-file my_texts.txt     # one URI per line
    python extract/fetch_openiti.py --master                     # every in-corpus OpenITI
                                                                 # row of metadata.xlsx
                                                                 # with no raw file yet
"""
import argparse
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "scripts"))
from settings import DATA, METADATA  # noqa: E402

RAW = "https://raw.githubusercontent.com/OpenITI/{repo}/master/{path}"
SUFFIXES = ("", ".mARkdown", ".completed", ".inProgress")
UA = {"User-Agent": "kashshaf-data-pipeline/1.0"}


def century_repo(version_uri: str) -> str | None:
    """`0630IbnCunayn...` -> `0650AH`: the death year rounded up to 25."""
    m = re.match(r"^(\d{4})", version_uri)
    if not m:
        return None
    death = int(m.group(1))
    return f"{max(25, math.ceil(death / 25) * 25):04d}AH"


def derived_path(version_uri: str) -> str | None:
    """`data/<author>/<author>.<book>/<version uri>` from the URI itself."""
    parts = version_uri.split(".")
    if len(parts) < 3:
        return None
    author, book = parts[0], parts[1]
    return f"data/{author}/{author}.{book}/{version_uri}"


def release_paths() -> dict[str, str]:
    """version_uri -> local_path, from the OpenITI release metadata."""
    import pandas as pd
    from collect_metadata import openiti_tsv

    t = pd.read_csv(openiti_tsv(), sep="\t", dtype=str, keep_default_na=False)
    return {u: p.replace("\\", "/") for u, p in zip(t["version_uri"], t["local_path"]) if p}


def fetch(url: str, timeout: int = 60) -> bytes | None:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except urllib.error.URLError:
        return None


def candidates(version_uri: str, paths: dict[str, str]) -> list[tuple[str, str]]:
    """Every (repo, path) worth trying, best first."""
    repo = century_repo(version_uri)
    if repo is None:
        return []
    out = []
    for path in dict.fromkeys(p for p in (paths.get(version_uri), derived_path(version_uri)) if p):
        out.append((repo, path))
        # A text can sit in the neighbouring repository.
        n = int(repo[:4])
        out += [(f"{other:04d}AH", path) for other in (n - 25, n + 25) if other >= 25]
    return out


def get_text(version_uri: str, paths: dict[str, str]) -> tuple[bytes, str] | None:
    """The bytes and the file name to save under."""
    for repo, path in candidates(version_uri, paths):
        for suffix in SUFFIXES:
            body = fetch(RAW.format(repo=repo, path=path + suffix))
            if body is not None:
                return body, os.path.basename(path + suffix)
    return None


def master_uris() -> list[str]:
    import pandas as pd

    m = pd.read_excel(METADATA, dtype=str, keep_default_na=False)
    return list(m[(m["corpus"] == "openiti") & (m["in_corpus"] == "1")]["original_id"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uris", nargs="+", help="version URIs to fetch")
    ap.add_argument("--uri-file", type=Path, help="a file with one version URI per line")
    ap.add_argument("--master", action="store_true", help="every in-corpus OpenITI row of metadata/metadata.xlsx")
    ap.add_argument("--out", type=Path, default=DATA / "raw/openiti")
    ap.add_argument("--sleep", type=float, default=0.15, help="pause between requests")
    args = ap.parse_args()

    if args.uris:
        targets = args.uris
    elif args.uri_file:
        targets = [ln.strip() for ln in args.uri_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    elif args.master:
        targets = master_uris()
    else:
        ap.error("give --uris, --uri-file or --master")

    args.out.mkdir(parents=True, exist_ok=True)
    have = os.listdir(args.out)
    todo = [u for u in targets if not any(n.startswith(u) for n in have)]
    print(f"{len(targets):,} texts asked for, {len(targets) - len(todo):,} already here, fetching {len(todo):,}")
    paths = release_paths() if todo else {}

    done = failed = 0
    started = time.time()
    for i, uri in enumerate(todo, 1):
        got = get_text(uri, paths)
        if got is None:
            failed += 1
            print(f"  FAILED  {uri}")
        else:
            body, name = got
            (args.out / name).write_bytes(body)
            done += 1
        if i % 25 == 0 or i == len(todo):
            print(f"  {i}/{len(todo)}  {done} fetched, {failed} failed  "
                  f"({i / max(1e-6, time.time() - started):.1f}/s)", flush=True)
        time.sleep(args.sleep)

    print(f"\nfetched {done}, failed {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
