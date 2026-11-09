#!/usr/bin/env python3
"""Run the whole pipeline on the short texts in tests/sample/.

    python tests/run_sample.py                            # all four sources
    python tests/run_sample.py --corpora openiti nusus    # only these
    python tests/run_sample.py --extract                  # try the extractors too
    python tests/run_sample.py --keep                     # keep the output folder
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "tests/sample"
sys.path.insert(0, str(ROOT / "scripts"))

from collect_metadata import openiti_uri  # noqa: E402
from settings import AHLBAYT_APP, SHAMELA_APP  # noqa: E402

CORPORA = ("openiti", "shamela", "ahlbayt", "nusus")
PY = sys.executable
OK, SKIPPED, FAILED = "ok", "skipped", "FAILED"


def run(cmd: list, env: dict, log: Path) -> bool:
    with open(log, "a", encoding="utf-8") as out:
        out.write("$ " + " ".join(str(c) for c in cmd) + "\n")
        out.flush()
        return subprocess.run([str(c) for c in cmd], cwd=ROOT, env=env, stdout=out, stderr=out).returncode == 0


def has_pages(folder: Path) -> bool:
    """A JSON text in `folder` with parts, pages and bodies."""
    for f in folder.glob("*.json"):
        book = json.loads(f.read_text(encoding="utf-8"))
        pages = [p for part in book.get("parts", []) for p in part.get("pages", [])]
        if pages and all("body" in p for p in pages):
            return True
    return False


def extract_openiti(out: Path, env: dict, log: Path) -> tuple[str, str]:
    try:
        urllib.request.urlopen("https://raw.githubusercontent.com", timeout=10)
    except urllib.error.HTTPError:
        pass  # the host answered
    except OSError:
        return SKIPPED, "no network"
    uri = openiti_uri(next((SAMPLE / "data/raw/openiti").iterdir()).name)
    if not run([PY, ROOT / "extract/fetch_openiti.py", "--uris", uri, "--out", out], env, log):
        return FAILED, "fetch_openiti.py failed"
    good = any("#META#Header#End#" in f.read_text(encoding="utf-8", errors="replace") for f in out.iterdir())
    return (OK, uri) if good else (FAILED, "downloaded file has no OpenITI header")


def extract_shamela(out: Path, env: dict, log: Path) -> tuple[str, str]:
    if not SHAMELA_APP or not SHAMELA_APP.exists():
        return SKIPPED, "no Shamela folder ([sources] shamela in config.toml)"
    if not shutil.which("java"):
        return SKIPPED, "Java is not installed"
    d = ROOT / "extract/shamela"
    if not run([PY, d / "extract_indices.py", "--test-single-book",
                "--shamela-path", SHAMELA_APP, "--output-path", out], env, log):
        return FAILED, "extract_indices.py failed"
    if not run([PY, d / "build_jsons.py", "--test-single-book", "--shamela-path", SHAMELA_APP,
                "--extracted-path", out, "--out", out / "json"], env, log):
        return FAILED, "build_jsons.py failed"
    return (OK, "one book") if has_pages(out / "json") else (FAILED, "no JSON with pages was written")


def extract_ahlbayt(out: Path, env: dict, log: Path) -> tuple[str, str]:
    if not AHLBAYT_APP or not AHLBAYT_APP.exists():
        return SKIPPED, "no Ahl al-Bayt app folder ([sources] ahlbayt in config.toml)"
    f = next((SAMPLE / "data/raw/ahlbayt").glob("*.json"))
    book = json.loads(f.read_text(encoding="utf-8"))["ab_book_ids"][0]
    if not run([PY, ROOT / "extract/ab_extractor.py", "--app-dir", AHLBAYT_APP, "--out", out,
                "--metadata", out / "ahlbayt.xlsx", "--ids", book], env, log):
        return FAILED, "ab_extractor.py failed"
    return (OK, f"book {book}") if has_pages(out) else (FAILED, "no JSON with pages was written")


EXTRACTORS = {"openiti": extract_openiti, "shamela": extract_shamela, "ahlbayt": extract_ahlbayt}


def prepare(work: Path, corpora: list[str]) -> dict:
    """The sample's texts and source tables in `work`, and an environment that points there."""
    for c in CORPORA:
        (work / "data/raw" / c).mkdir(parents=True)
    (work / "metadata/sources").mkdir(parents=True)
    for c in corpora:
        shutil.copytree(SAMPLE / "data/raw" / c, work / "data/raw" / c, dirs_exist_ok=True)
    if "openiti" in corpora:
        shutil.copy(SAMPLE / "metadata/sources/OpenITI_metadata_sample.tsv", work / "metadata/sources")
    if "ahlbayt" in corpora:
        shutil.copy(SAMPLE / "metadata/sources/ahlbayt.xlsx", work / "metadata/sources")
    config = work / "config.toml"
    config.write_text(f'[paths]\ndata = "{(work / "data").as_posix()}"\n'
                      f'metadata = "{(work / "metadata").as_posix()}"\n', encoding="utf-8")
    return os.environ | {"KASHSHAF_CONFIG": str(config), "PYTHONIOENCODING": "utf-8"}


def build(work: Path, env: dict, log: Path) -> tuple[str, str]:
    if not run([PY, ROOT / "scripts/collect_metadata.py"], env, log):
        return FAILED, "collect_metadata.py failed"
    if not run([PY, ROOT / "scripts/build_corpus.py", "0.0.1"], env, log):
        return FAILED, "build_corpus.py failed"
    con = sqlite3.connect(work / "data/build/0.0.1/corpus.db")
    books, pages, tokens = con.execute("SELECT book_count, page_count, token_count FROM db_info").fetchone()
    con.close()
    return OK, f"{books} books, {pages} pages, {tokens} tokens"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpora", nargs="+", choices=CORPORA, default=list(CORPORA))
    ap.add_argument("--extract", action="store_true", help="also try each extractor on one text")
    ap.add_argument("--keep", action="store_true", help="keep the output folder")
    a = ap.parse_args()

    if not list((ROOT / "indexer/target/release").glob("kashshaf-indexer*")):
        sys.exit("the indexer is not built: run `cargo build --release` in indexer/")

    work = Path(tempfile.mkdtemp(prefix="kashshaf-sample-"))
    log = work / "run.log"
    results = []
    env = prepare(work, a.corpora)
    print(f"Running in {work}; this takes a minute or two.")

    if a.extract:
        for c in a.corpora:
            if c not in EXTRACTORS:
                results.append((f"extract {c}", SKIPPED, "this source has no extractor"))
                continue
            out = work / "extract" / c
            out.mkdir(parents=True)
            results.append((f"extract {c}", *EXTRACTORS[c](out, env, log)))

    results.append(("build " + " ".join(a.corpora), *build(work, env, log)))

    print()
    for name, status, note in results:
        print(f"  {status:8} {name:40} {note}")
    failed = any(status == FAILED for _, status, _ in results)
    if failed or a.keep:
        print(f"\nOutput and log kept in {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
