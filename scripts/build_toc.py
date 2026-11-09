#!/usr/bin/env python3
"""Build toc.db — every book's table of contents — from the corpus JSONL."""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import orjson as _json

    def loads(b: bytes):
        return _json.loads(b)
except ImportError:
    import json as _json

    def loads(b: bytes):
        return _json.loads(b)

sys.stdout.reconfigure(encoding="utf-8")

SCHEMA_VERSION = 1

OPEN_RE = re.compile(r"<title\s+id=(\d+)\s+parent=(\d+)\s*>")
TAG_RE = re.compile(r"<[^>]*>")
WS_RE = re.compile(r"\s+")
MAX_TITLE = 500


def clean(text: str) -> str:
    """Heading text as it should be shown: markup out, whitespace collapsed."""
    return WS_RE.sub(" ", TAG_RE.sub(" ", text)).strip()[:MAX_TITLE]


def headings(body: str):
    """Every `<title>` of one page as `(id, parent, text)`, in order."""
    out = []
    for m in OPEN_RE.finditer(body):
        start = m.end()
        close = body.find("</title>", start)
        if close == -1:
            nxt = body.find("<", start)
            close = nxt if nxt != -1 else len(body)
        out.append((int(m.group(1)), int(m.group(2)), clean(body[start:close])))
    return out


def corpus_version(corpus_db: Path | None) -> str | None:
    if not corpus_db or not corpus_db.exists():
        return None
    con = sqlite3.connect(f"file:{corpus_db}?mode=ro", uri=True)
    try:
        return con.execute("SELECT corpus_version FROM db_info").fetchone()[0]
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jsonl-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--corpus-db", type=Path, default=None, help="stamps toc.db with its corpus_version")
    args = ap.parse_args()

    files = sorted(args.jsonl_dir.glob("*.jsonl"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0)
    if not files:
        sys.exit(f"no JSONL files in {args.jsonl_dir}")
    version = corpus_version(args.corpus_db)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out.exists():
        args.out.unlink()
    con = sqlite3.connect(args.out)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.executescript(
        """
        CREATE TABLE toc (
            book_id     INTEGER NOT NULL,
            id          INTEGER NOT NULL,
            parent      INTEGER NOT NULL,
            title       TEXT    NOT NULL,
            part_index  INTEGER NOT NULL,
            page_id     INTEGER NOT NULL,
            page_number TEXT    NOT NULL
        );
        CREATE TABLE db_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )

    started = time.time()
    rows: list[tuple] = []
    books_with = 0
    total = 0
    for i, path in enumerate(files):
        book_rows = []
        seen: set[int] = set()
        with path.open("rb") as f:
            for raw in f:
                if not raw.strip():
                    continue
                d = loads(raw)
                book_id = int(d["id"])
                part_index = int(d.get("part_index", 0))
                page_id = int(d["page_id"])
                page_number = str(d.get("page_number", "") or "")
                for tid, parent, title in headings(d.get("body", "")):
                    # A heading split over two pages appears once, at its start.
                    if tid in seen:
                        continue
                    seen.add(tid)
                    book_rows.append((book_id, tid, parent, title, part_index, page_id, page_number))
        if book_rows:
            books_with += 1
            rows.extend(book_rows)
            total += len(book_rows)
        if len(rows) >= 200_000:
            con.executemany("INSERT INTO toc VALUES (?,?,?,?,?,?,?)", rows)
            rows.clear()
        if (i + 1) % 500 == 0 or i + 1 == len(files):
            print(f"  {i + 1}/{len(files)} books, {total:,} entries, {time.time() - started:.0f}s", flush=True)
    if rows:
        con.executemany("INSERT INTO toc VALUES (?,?,?,?,?,?,?)", rows)

    print("  indexing…", flush=True)
    con.executescript(
        """
        CREATE INDEX toc_parent ON toc(book_id, parent);
        """
    )
    con.executemany(
        "INSERT INTO db_info VALUES (?,?)",
        [
            ("schema_version", str(SCHEMA_VERSION)),
            ("corpus_version", version or ""),
            ("built_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            ("book_count", str(len(files))),
            ("books_with_toc", str(books_with)),
            ("entry_count", str(total)),
        ],
    )
    con.commit()
    con.execute("VACUUM")
    con.close()

    size = args.out.stat().st_size
    print(
        f"Wrote {args.out} — {total:,} entries over {books_with:,}/{len(files):,} books, "
        f"{size / 1024 / 1024:.1f} MB, {time.time() - started:.0f}s"
        + (f", corpus {version}" if version else "")
    )


if __name__ == "__main__":
    main()
