#!/usr/bin/env python3
"""Build a corpus end to end, resuming where it stopped.

    python scripts/build_corpus.py 1.0.0
    python scripts/build_corpus.py 1.0.0 --books 7548 7549   # only these books
    python scripts/build_corpus.py 1.0.0 --from indexer      # skip ahead
    python scripts/build_corpus.py 1.0.0 --list              # what would run
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from settings import DATA, METADATA

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
PY = sys.executable

COLUMNS = ["id", "corpus", "original_id", "in_corpus", "title", "author_id",
           "author_name", "death_ah", "century_ah", "genre_id", "genre", "paginated",
           "page_count", "token_count", "tags", "book_meta", "author_meta"]
REQUIRED_VALUES = ["id", "corpus", "original_id", "title", "author_id",
                   "author_name", "paginated"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"stages": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    def done(self, stage: str) -> bool:
        return self.data["stages"].get(stage, {}).get("ok") is True

    def record(self, stage: str, seconds: float) -> None:
        self.data["stages"][stage] = {
            "ok": True,
            "seconds": round(seconds, 1),
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")


def run(cmd: list, cwd: Path | None = None) -> None:
    printable = " ".join(str(c) for c in cmd)
    log(f"$ {printable}")
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd or ROOT))
    if r.returncode != 0:
        die(f"stage failed ({r.returncode}): {printable}")


def binary(name: str) -> Path:
    for exe in (f"{name}.exe", name):
        p = ROOT / "indexer/target/release" / exe
        if p.exists():
            return p
    die(f"{name} not built: run `cargo build --release` in indexer/")


def check_metadata(path: Path = METADATA) -> list[str]:
    """What is wrong with metadata.xlsx, as a list of messages; empty if nothing."""
    import pandas as pd

    if not path.exists():
        return [f"{path} not found: run scripts/collect_metadata.py first"]
    df = pd.read_excel(path, dtype=str, keep_default_na=False)
    problems = [f"missing column `{c}`" for c in COLUMNS if c not in df.columns]
    if problems:
        return problems
    live = df[df["in_corpus"].str.strip() == "1"]
    if live.empty:
        problems.append("no row has in_corpus = 1")
    for c in REQUIRED_VALUES:
        blank = live[live[c].str.strip() == ""]
        if len(blank):
            problems.append(f"{len(blank)} in-corpus rows have no `{c}`, e.g. "
                            + ", ".join(f"{r.corpus}.{r.original_id}" for r in blank.head(5).itertuples()))
    dup = df[df["id"].duplicated(keep=False) & (df["id"] != "")]
    if len(dup):
        problems.append(f"`id` repeats: {sorted(set(dup['id']))[:10]}")
    dup = df[df.duplicated(["corpus", "original_id"], keep=False)]
    if len(dup):
        problems.append(f"{len(dup)} rows share a corpus and original_id, e.g. "
                        + ", ".join(sorted(set(dup["corpus"] + "." + dup["original_id"]))[:5]))
    half = df[(df["genre_id"].str.strip() == "") != (df["genre"].str.strip() == "")]
    if len(half):
        problems.append(f"{len(half)} rows have only one of `genre_id` and `genre`, e.g. "
                        + ", ".join(f"{r.corpus}.{r.original_id}" for r in half.head(5).itertuples()))
    for id_col, name_col in (("author_id", "author_name"), ("genre_id", "genre")):
        pairs = df[[id_col, name_col]][df[id_col] != ""].drop_duplicates()
        clash = sorted(set(pairs[id_col][pairs[id_col].duplicated()]))
        if clash:
            problems.append(f"`{id_col}` with more than one `{name_col}`: {clash[:10]}")
    return problems


def stage_check(a, out: Path) -> None:
    problems = check_metadata()
    for p in problems:
        print(f"  {p}")
    if problems:
        die(f"metadata/metadata.xlsx is not ready ({len(problems)} problem(s)); see README.md, stage 2")
    log("  metadata.xlsx is ready")


def stage_pipeline(a, out: Path) -> None:
    run([PY, SCRIPTS / "kashshaf_pipeline.py",
         "--openiti-dir", DATA / "raw/openiti",
         "--shamela-dir", DATA / "raw/shamela",
         "--ahlbayt-dir", DATA / "raw/ahlbayt",
         "--nusus-dir", DATA / "raw/nusus",
         "--output-dir", DATA / "canonical",
         "--metadata", METADATA,
         "--cleaner", a.cleaner])


def canonical_files(book_ids: list[int]) -> list[Path]:
    """data/canonical/<corpus>.<original_id>.json for each book id."""
    import pandas as pd

    m = pd.read_excel(METADATA, dtype=str, keep_default_na=False).set_index("id")
    files = []
    for b in book_ids:
        if str(b) not in m.index:
            die(f"book {b} is not in metadata.xlsx")
        r = m.loc[str(b)]
        files.append(DATA / "canonical" / f"{r['corpus']}.{r['original_id']}.json")
    return files


def stage_process_batch(a, out: Path) -> None:
    base = [PY, SCRIPTS / "process_batch.py",
            "--input-dir", DATA / "canonical",
            "--output-dir", DATA / "processed",
            "--metadata", METADATA]
    if a.books:
        for f in canonical_files(a.books):
            run(base + ["--book", f])
    else:
        run(base + ["--all"])


def stage_sqlite(a, out: Path) -> None:
    base = [PY, SCRIPTS / "build_sqlite_tokens.py", "--data-dir", out,
            "--jsonl-dir", DATA / "processed"]
    run(base + ["--load-metadata", "--corpus-version", a.version])
    if a.books:
        run(base + ["--add", "--books"] + [str(b) for b in a.books] +
            ["--corpus-version", a.version])
    else:
        run(base + ["--rebuild", "--corpus-version", a.version])
    run(base + ["--recompress", "--zstd-level", "9"])
    run(base + ["--build-triples"])


def stage_metadata(a, out: Path) -> None:
    """Reload metadata.db alone into a built corpus (`--only metadata`)."""
    if not (out / "corpus.db").exists():
        die(f"{out} has no corpus.db: build the corpus before reloading its metadata")
    run([PY, SCRIPTS / "build_sqlite_tokens.py", "--data-dir", out,
         "--jsonl-dir", DATA / "processed",
         "--load-metadata", "--keep-corpus-db", "--corpus-version", a.version])


def stage_indexer(a, out: Path) -> None:
    run([binary("kashshaf-indexer"), "--rebuild", "--compound", "--keep-root-text",
         "--input", DATA / "processed",
         "--corpus-db", out / "corpus.db",
         "--metadata-db", out / "metadata.db",
         "--index", out / "tantivy_index",
         "--zstd-level", "3"])


def stage_check_order(a, out: Path) -> None:
    run([binary("check_order"), "--index", out / "tantivy_index"])


def stage_boundary(a, out: Path) -> None:
    """One document per page break, so phrases can be found across one."""
    run([PY, SCRIPTS / "build_boundary_index.py", "--data-dir", out, "--corpus-version", a.version])
    run([PY, SCRIPTS / "build_boundary_index.py", "--data-dir", out, "--check"])


def stage_toc(a, out: Path) -> None:
    run([PY, SCRIPTS / "build_toc.py",
         "--jsonl-dir", DATA / "processed",
         "--out", out / "toc.db",
         "--corpus-db", out / "corpus.db"])


STAGES = [
    ("check", stage_check),
    ("pipeline", stage_pipeline),
    ("process_batch", stage_process_batch),
    ("sqlite", stage_sqlite),
    ("indexer", stage_indexer),
    ("check_order", stage_check_order),
    ("boundary", stage_boundary),
    ("toc", stage_toc),
]
EXTRA_STAGES = [
    ("metadata", stage_metadata),
]


def main() -> int:
    names = [n for n, _ in STAGES]
    extra = dict(EXTRA_STAGES)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", help="corpus version to build, e.g. 1.0.0")
    ap.add_argument("--books", type=int, nargs="+", help="incremental: only these book ids")
    ap.add_argument("--from", dest="start", choices=names, help="start at this stage")
    ap.add_argument("--only", choices=names + list(extra),
                    help="run just this stage; `metadata` reloads metadata.db alone into a built corpus")
    ap.add_argument("--force", action="store_true", help="ignore the checkpoint")
    ap.add_argument("--list", action="store_true", help="print the plan and stop")
    ap.add_argument("--cleaner", choices=["v1", "v2"], default="v2")
    a = ap.parse_args()

    out = DATA / "build" / a.version
    state = State(out / ".build_state.json")

    plan = STAGES
    if a.only:
        plan = [(n, f) for n, f in STAGES + EXTRA_STAGES if n == a.only]
    elif a.start:
        plan = STAGES[names.index(a.start):]

    if a.list:
        print(f"corpus {a.version} -> {out}")
        print(f"mode: {'incremental, ' + str(len(a.books)) + ' books' if a.books else 'full rebuild'}")
        for n, _ in plan:
            mark = "done" if state.done(n) and not a.force else ""
            print(f"  {n:15} {mark}")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    log(f"corpus {a.version} -> {out}")
    log(f"{'incremental: ' + ' '.join(str(b) for b in a.books) if a.books else 'full rebuild'}")

    for name, fn in plan:
        # Always runs: the spreadsheet may have changed.
        if state.done(name) and not a.force and not a.only and name != "check":
            log(f"{name}: done already, skipping")
            continue
        log(f"--- {name} ---")
        t0 = time.time()
        fn(a, out)
        state.record(name, time.time() - t0)
        log(f"--- {name} ok in {time.time() - t0:.0f}s ---")

    log(f"build complete: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
