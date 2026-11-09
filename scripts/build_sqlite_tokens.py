#!/usr/bin/env python
"""Build corpus.db and metadata.db from processed book JSONL files."""

import argparse
import json
import sqlite3
import sys
import time
import threading
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from multiprocessing import Process, Queue, cpu_count
from pathlib import Path
from queue import Empty
from typing import Optional

import pandas as pd

from settings import DATA, METADATA

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = DATA
BOOKS_DIR = DATA / "processed"
CORPUS_DB_PATH = DATA / "corpus.db"
METADATA_DB_PATH = DATA / "metadata.db"
METADATA_XLSX_PATH = METADATA


def set_data_dir(data_dir: Path) -> None:
    """Point every data path at a different directory (e.g. data/sample)."""
    global DATA_DIR, BOOKS_DIR, CORPUS_DB_PATH, METADATA_DB_PATH
    DATA_DIR = Path(data_dir).resolve()
    BOOKS_DIR = DATA_DIR / "processed"
    CORPUS_DB_PATH = DATA_DIR / "corpus.db"
    METADATA_DB_PATH = DATA_DIR / "metadata.db"


REPORT_INTERVAL = 2.0
PAGE_BATCH_SIZE = 500
COMMIT_INTERVAL = 5000
# db_info.schema_version: 1 original, 2 `parts`, 3 encoding + token_codec, 4 triples.
# Read from what a database holds, never stamped: the writers run in no fixed order.
SCHEMA_VERSION = 3

# metadata.db is pinned: its layout has not changed since `parts`.
METADATA_SCHEMA_VERSION = 4


def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _has_column(conn, table: str, column: str) -> bool:
    if not _has_table(conn, table):
        return False
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def corpus_schema_version(conn) -> int:
    """What this corpus.db actually is, from its tables."""
    if _has_table(conn, "triples") and _has_column(conn, "token_definitions", "triple_id"):
        return 4
    if _has_column(conn, "page_tokens", "encoding") and _has_table(conn, "token_codec"):
        return 3
    if _has_table(conn, "page_tokens"):
        return 2
    return 1

ENCODING_RAW_U32 = 1
ENCODING_RANK_VARINT_ZSTD = 2

DEFAULT_ZSTD_LEVEL = 9
DEFAULT_DICT_SIZE = 110 * 1024
DEFAULT_DICT_SAMPLES = 20_000
DEFAULT_VERIFY_SAMPLE = 2_000
RECOMPRESS_CHUNK_PAGES = 20_000

DEFAULT_NUM_WORKERS = max(1, cpu_count() - 2)
QUEUE_SIZE = 200
TASK_QUEUE_SIZE = 50

DONE_SENTINEL = "__DONE__"

CORPUS_SCHEMA = """
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS roots (
    id      INTEGER PRIMARY KEY,
    root    TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS lemmas (
    id      INTEGER PRIMARY KEY,
    lemma   TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS pos_types (
    id      INTEGER PRIMARY KEY,
    pos     TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_sets (
    id          INTEGER PRIMARY KEY,
    features    TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS clitic_sets (
    id          INTEGER PRIMARY KEY,
    clitics     TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS token_definitions (
    id              INTEGER PRIMARY KEY,
    surface         TEXT NOT NULL,
    lemma_id        INTEGER NOT NULL,
    root_id         INTEGER,
    pos_id          INTEGER NOT NULL,
    feature_set_id  INTEGER NOT NULL,
    clitic_set_id   INTEGER NOT NULL,
    UNIQUE(surface, lemma_id, root_id, pos_id, feature_set_id, clitic_set_id)
);

CREATE TABLE IF NOT EXISTS page_tokens (
    book_id     INTEGER NOT NULL,
    part_index  INTEGER NOT NULL,
    page_id     INTEGER NOT NULL,
    encoding    INTEGER NOT NULL DEFAULT 1,
    token_ids   BLOB NOT NULL,
    PRIMARY KEY (book_id, part_index, page_id)
) WITHOUT ROWID;

-- Codec artifacts for encoding 2 (see module docstring):
--   key = 'rank_to_def'  data = uint32 LE array, index = rank, value = token_definitions.id
--   key = 'zstd_dict'    data = trained zstd dictionary bytes
-- `meta` is a JSON string with provenance (level, dict size, counts, created_at).
CREATE TABLE IF NOT EXISTS token_codec (
    key     TEXT PRIMARY KEY,
    data    BLOB NOT NULL,
    meta    TEXT
);

-- The app queries token_definitions by id, lemma_id and root_id only.
-- (idx_token_def_surface was dropped in schema v3; it was never used at runtime.)
CREATE INDEX IF NOT EXISTS idx_token_def_lemma ON token_definitions(lemma_id);
CREATE INDEX IF NOT EXISTS idx_token_def_root  ON token_definitions(root_id);

CREATE TABLE IF NOT EXISTS db_info (
    corpus_version  TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    built_at        TEXT NOT NULL,
    book_count      INTEGER NOT NULL,
    page_count      INTEGER NOT NULL,
    token_count     INTEGER NOT NULL
);
"""

METADATA_SCHEMA = """
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS authors (
    id      INTEGER PRIMARY KEY,
    author  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS genres (
    id      INTEGER PRIMARY KEY,
    genre   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY,
    corpus      TEXT,
    title       TEXT NOT NULL,
    author_id   INTEGER REFERENCES authors(id),
    death_ah    INTEGER,
    century_ah  INTEGER,
    genre_id    INTEGER REFERENCES genres(id),
    page_count  INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    parts       INTEGER NOT NULL DEFAULT 0,
    original_id TEXT,
    paginated   INTEGER,
    tags        TEXT,
    book_meta   TEXT,
    author_meta            TEXT,
    metadata_json TEXT,
    citation_json          TEXT,
    in_corpus              INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS db_info (
    corpus_version  TEXT NOT NULL,
    schema_version  INTEGER NOT NULL,
    built_at        TEXT NOT NULL,
    book_count      INTEGER NOT NULL,
    page_count      INTEGER NOT NULL,
    token_count     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_books_author ON books(author_id);
CREATE INDEX IF NOT EXISTS idx_books_genre ON books(genre_id);
"""


def _ensure_parts_column(meta_conn: sqlite3.Connection) -> None:
    """Add columns introduced after schema v1 if they're missing."""
    cursor = meta_conn.cursor()
    cursor.execute("PRAGMA table_info(books)")
    cols = {row[1] for row in cursor.fetchall()}

    if 'parts' not in cols:
        print("  Migrating: adding `parts` column to books table...")
        cursor.execute(
            "ALTER TABLE books ADD COLUMN parts INTEGER NOT NULL DEFAULT 0"
        )

    if 'metadata_json' not in cols:
        print("  Migrating: adding `metadata_json` column...")
        cursor.execute(
            "ALTER TABLE books ADD COLUMN metadata_json TEXT"
        )

    if 'citation_json' not in cols:
        print("  Migrating: adding `citation_json` column...")
        cursor.execute(
            "ALTER TABLE books ADD COLUMN citation_json TEXT"
        )

    meta_conn.commit()


def _read_existing_db_info(conn: sqlite3.Connection) -> Optional[tuple[str, int]]:
    """Return (corpus_version, schema_version) from db_info if present."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT corpus_version, schema_version FROM db_info LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            return (row[0], row[1])
    except sqlite3.OperationalError:
        pass
    return None


def _write_db_info(
    conn: sqlite3.Connection,
    corpus_version: str,
    schema_version: int,
    book_count: int,
    page_count: int,
    token_count: int,
) -> None:
    """Replace the single row in db_info with current values."""
    cur = conn.cursor()
    built_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    cur.execute("DELETE FROM db_info")
    cur.execute(
        """INSERT INTO db_info
           (corpus_version, schema_version, built_at,
            book_count, page_count, token_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (corpus_version, schema_version, built_at,
         book_count, page_count, token_count)
    )
    conn.commit()


def load_metadata(meta_conn: sqlite3.Connection, version: str):
    """Load books, authors and genres from metadata.xlsx into metadata.db."""
    cursor = meta_conn.cursor()

    print(f"Loading metadata from {METADATA_XLSX_PATH.name} into metadata.db...")
    df_books = pd.read_excel(METADATA_XLSX_PATH)

    for table, id_col, name_col, out_col in (("authors", "author_id", "author_name", "author"),
                                             ("genres", "genre_id", "genre", "genre")):
        pairs = df_books[[id_col, name_col]].dropna().drop_duplicates()
        clash = pairs[pairs.duplicated(id_col, keep=False)]
        if len(clash):
            raise SystemExit(f"{METADATA_XLSX_PATH.name}: {id_col} with more than one {name_col}:\n"
                             f"{clash.sort_values(id_col).head(20).to_string(index=False)}")
        cursor.execute(f"DELETE FROM {table}")
        cursor.executemany(f"INSERT INTO {table} (id, {out_col}) VALUES (?, ?)",
                           [(int(i), str(n)) for i, n in zip(pairs[id_col], pairs[name_col])])
        print(f"    Loaded {len(pairs):,} {table}")

    cursor.execute("DELETE FROM books")

    df_books['token_count'] = df_books['token_count'].fillna(0).astype(int)
    df_books['page_count'] = df_books['page_count'].fillna(0).astype(int)
    if 'in_corpus' in df_books.columns:
        df_books['in_corpus'] = df_books['in_corpus'].fillna(True).astype(bool).astype(int)
    else:
        df_books['in_corpus'] = 1

    in_corpus_count = int(df_books['in_corpus'].sum())
    excluded_count = len(df_books) - in_corpus_count

    for _, row in df_books.iterrows():
        # A death date <= 0 means none (Shamela has -1); a blank century follows from the date.
        death_ah = int(row['death_ah']) if pd.notna(row['death_ah']) and int(row['death_ah']) > 0 else None
        century_ah = (int(row['century_ah']) if pd.notna(row['century_ah']) and int(row['century_ah']) > 0
                      else (death_ah // 100 + 1 if death_ah else None))
        smj = (row['metadata_json']
               if 'metadata_json' in df_books.columns
               and pd.notna(row['metadata_json'])
               else None)
        cj = (row['citation_json']
              if 'citation_json' in df_books.columns
              and pd.notna(row['citation_json'])
              else None)

        cursor.execute(
            """INSERT INTO books (id, corpus, title, author_id, death_ah, century_ah,
               genre_id, page_count, token_count, parts, original_id, paginated, tags,
               book_meta, author_meta, metadata_json, citation_json, in_corpus)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(row['id']),
                row['corpus'] if pd.notna(row['corpus']) else None,
                row['title'],
                int(row['author_id']) if pd.notna(row['author_id']) else None,
                death_ah,
                century_ah,
                int(row['genre_id']) if pd.notna(row['genre_id']) else None,
                int(row['page_count']),
                int(row['token_count']),
                0,
                row['original_id'] if pd.notna(row['original_id']) else None,
                int(row['paginated']) if pd.notna(row['paginated']) else None,
                row['tags'] if pd.notna(row['tags']) else None,
                row['book_meta'] if pd.notna(row['book_meta']) else None,
                row['author_meta'] if pd.notna(row['author_meta']) else None,
                smj,
                cj,
                int(row['in_corpus'])
            )
        )
    print(f"    Loaded {len(df_books):,} books "
          f"({in_corpus_count:,} in_corpus=1, {excluded_count:,} in_corpus=0)")

    print("  Updating db_info (initial — refreshed by update_token_counts())...")
    _write_db_info(
        meta_conn,
        corpus_version=version,
        schema_version=METADATA_SCHEMA_VERSION,
        book_count=len(df_books),
        page_count=int(df_books['page_count'].sum()),
        token_count=int(df_books['token_count'].sum()),
    )
    print(f"    Version: {version} (metadata.db schema v{METADATA_SCHEMA_VERSION})")

    print("  Metadata loading complete.\n")


@dataclass
class ProcessedPage:
    """A page ready for database insertion."""
    book_id: int
    part_index: int
    page_id: int
    part_label: Optional[str]
    page_number: Optional[str]
    tokens: list[tuple[str, str, Optional[str], str, str, str]]


@dataclass
class ProcessedBook:
    """A fully processed book ready for database insertion."""
    book_id: int
    pages: list[ProcessedPage] = field(default_factory=list)
    error: Optional[str] = None


def process_book_file(book_file: Path) -> ProcessedBook:
    """Parse a book JSONL file in a worker process (no database access)."""
    book_id = int(book_file.stem)
    pages = []

    seen_pages = set()
    last_page_id = {}

    with open(book_file, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            page = json.loads(line)

            part = page["part_index"]
            pid = page["page_id"]

            key = (book_id, part, pid)
            if key in seen_pages:
                return ProcessedBook(
                    book_id=book_id,
                    error=f"DUPLICATE PAGE KEY: {key}"
                )
            seen_pages.add(key)

            prev = last_page_id.get(part)
            if prev is not None and pid <= prev:
                return ProcessedBook(
                    book_id=book_id,
                    error=f"NON-MONOTONIC page_id in book {book_id}, part {part}: {pid} after {prev}"
                )
            last_page_id[part] = pid

            tokens = []
            for tok in page.get("tokens", []):
                surface = tok.get("surface", "")
                lemma = tok.get("lemma", "")
                root = tok.get("root")
                pos = tok.get("pos", "UNK")

                feats = tok.get("features")
                clits = tok.get("clitics")

                feats_json = (
                    json.dumps(feats, ensure_ascii=False, sort_keys=True)
                    if feats else "[]"
                )
                clits_json = (
                    json.dumps(clits, ensure_ascii=False, sort_keys=True)
                    if clits else "[]"
                )

                tokens.append((surface, lemma, root, pos, feats_json, clits_json))

            pages.append(ProcessedPage(
                book_id=book_id,
                part_index=part,
                page_id=pid,
                part_label=page.get("part_label"),
                page_number=page.get("page_number"),
                tokens=tokens,
            ))

    return ProcessedBook(
        book_id=book_id,
        pages=pages
    )


def worker_process(task_queue: Queue, result_queue: Queue, worker_id: int):
    """Worker process: reads book file paths from task_queue,
    processes them, and sends ProcessedBook objects to result_queue."""
    while True:
        try:
            item = task_queue.get(timeout=2)
        except Empty:
            continue

        if item == DONE_SENTINEL:
            result_queue.put(DONE_SENTINEL)
            break

        book_file = Path(item)
        try:
            book = process_book_file(book_file)
            result_queue.put(book)
        except Exception as e:
            book_id = int(book_file.stem) if book_file.stem.isdigit() else 0
            result_queue.put(ProcessedBook(
                book_id=book_id,
                error=f"Worker {worker_id} exception: {e}"
            ))


# --keep-corpus-db opens corpus.db read-only, so its published hash does not change.
KEEP_CORPUS_DB = False


def open_corpus_for_counts() -> sqlite3.Connection:
    """corpus.db for a count refresh: read-only under --keep-corpus-db."""
    if KEEP_CORPUS_DB:
        return sqlite3.connect(f"file:{CORPUS_DB_PATH.as_posix()}?mode=ro", uri=True)
    return connect_db(CORPUS_DB_PATH, large_cache=True)


def connect_db(path: Path, large_cache: bool = True) -> sqlite3.Connection:
    """Connect to a SQLite database with optimized settings."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    if large_cache:
        conn.execute("PRAGMA cache_size=-500000")
        conn.execute("PRAGMA mmap_size=30000000000")
    return conn


def init_corpus_schema(conn: sqlite3.Connection):
    """Initialize corpus.db schema (token data)."""
    conn.executescript(CORPUS_SCHEMA)
    conn.commit()


def _page_tokens_has_encoding_column(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(page_tokens)")
    return any(row[1] == "encoding" for row in cur.fetchall())


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    )
    return cur.fetchone() is not None


def varint_encode_int(n: int) -> bytes:
    """LEB128-encode a single non-negative integer."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def varint_decode_int(buf: bytes, pos: int = 0) -> tuple[int, int]:
    """Decode one LEB128 integer at `pos`. Returns (value, next_pos)."""
    result = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 35:
            raise ValueError("varint too long")


def leb128_encode_array(values) -> "np.ndarray":
    """Vectorised LEB128 encoding of a uint32 array -> uint8 array."""
    import numpy as np

    v = np.asarray(values, dtype=np.uint64)
    nbytes = leb128_byte_lengths(v)
    starts = np.cumsum(nbytes) - nbytes
    out = np.empty(int(nbytes.sum()), dtype=np.uint8)
    for k in range(5):
        mask = nbytes > k
        if not mask.any():
            break
        chunk = (v[mask] >> np.uint64(7 * k)) & np.uint64(0x7F)
        cont = (nbytes[mask] > k + 1).astype(np.uint64) << np.uint64(7)
        out[starts[mask] + k] = (chunk | cont).astype(np.uint8)
    return out


def leb128_byte_lengths(v) -> "np.ndarray":
    import numpy as np

    v = np.asarray(v, dtype=np.uint64)
    nbytes = np.ones(len(v), dtype=np.int64)
    nbytes += v >= np.uint64(1 << 7)
    nbytes += v >= np.uint64(1 << 14)
    nbytes += v >= np.uint64(1 << 21)
    nbytes += v >= np.uint64(1 << 28)
    return nbytes


def leb128_decode_bytes(buf: bytes, expected: int) -> list[int]:
    """Decode `expected` LEB128 integers from `buf` (pure Python; for
    verification and fixtures only, not the hot path)."""
    out = []
    pos = 0
    for _ in range(expected):
        val, pos = varint_decode_int(buf, pos)
        out.append(val)
    if pos != len(buf):
        raise ValueError(f"trailing bytes after {expected} varints")
    return out


def blob_token_count(encoding: int, blob: bytes) -> int:
    """Number of tokens in a page blob without fully decoding it."""
    if encoding == ENCODING_RAW_U32:
        return len(blob) // 4
    if encoding == ENCODING_RANK_VARINT_ZSTD:
        n, _ = varint_decode_int(blob, 0)
        return n
    raise ValueError(f"unknown page_tokens encoding {encoding}")


def decode_blob_raw(blob: bytes) -> list[int]:
    return list(array("I", blob)) if len(blob) % 4 == 0 else list(array("I", blob[: len(blob) // 4 * 4]))


class BlobCodecV2:
    """Encoder/decoder for encoding 2, bound to a rank map and dictionary."""

    def __init__(self, rank_to_def, dict_bytes: bytes, level: int):
        import numpy as np
        import zstandard as zstd

        self.rank_to_def = np.asarray(rank_to_def, dtype=np.uint32)
        self.def_to_rank = np.zeros(int(self.rank_to_def.max()) + 1, dtype=np.uint32)
        self.def_to_rank[self.rank_to_def] = np.arange(len(self.rank_to_def), dtype=np.uint32)
        self.dict_bytes = dict_bytes
        self.level = level
        cdict = zstd.ZstdCompressionDict(dict_bytes)
        params = zstd.ZstdCompressionParameters.from_level(
            level, write_checksum=0, write_content_size=0, write_dict_id=0
        )
        self._cctx = zstd.ZstdCompressor(dict_data=cdict, compression_params=params)
        self._dctx = zstd.ZstdDecompressor(dict_data=cdict)

    def encode_batch(self, raw_blobs: list[bytes]) -> list[bytes]:
        """Encode many raw (encoding 1) blobs. Returns encoding-2 blobs."""
        payloads, lengths = rank_varint_payloads(raw_blobs, self.def_to_rank)
        return [
            varint_encode_int(int(n)) + self._cctx.compress(p)
            for p, n in zip(payloads, lengths)
        ]

    def decode(self, blob: bytes) -> list[int]:
        """Decode one encoding-2 blob back to token_definitions ids."""
        n, pos = varint_decode_int(blob, 0)
        if n == 0:
            return []
        payload = self._dctx.decompress(blob[pos:], max_output_size=n * 5)
        ranks = leb128_decode_bytes(payload, n)
        return [int(self.rank_to_def[r]) for r in ranks]


def rank_varint_payloads(raw_blobs: list[bytes], def_to_rank) -> tuple[list[bytes], list[int]]:
    """Map many raw u32 blobs to (LEB128-of-rank payload, n_tokens) pairs."""
    import numpy as np

    if not raw_blobs:
        return [], []
    lengths = np.fromiter(
        (len(b) // 4 for b in raw_blobs), dtype=np.int64, count=len(raw_blobs)
    )
    total = int(lengths.sum())
    if total == 0:
        return [b"" for _ in raw_blobs], [0] * len(raw_blobs)

    concat = np.frombuffer(b"".join(raw_blobs), dtype="<u4")
    ranks = def_to_rank[concat]
    nbytes = leb128_byte_lengths(ranks)
    encoded = leb128_encode_array(ranks).tobytes()

    cum = np.concatenate(([0], np.cumsum(nbytes)))
    offsets = np.concatenate(([0], np.cumsum(lengths)))
    starts = cum[offsets[:-1]]
    ends = cum[offsets[1:]]
    payloads = [encoded[int(s):int(e)] for s, e in zip(starts, ends)]
    return payloads, lengths.tolist()


def decode_blob(encoding: int, blob: bytes, codec: Optional["BlobCodecV2"]) -> list[int]:
    if encoding == ENCODING_RAW_U32:
        return decode_blob_raw(blob)
    if encoding == ENCODING_RANK_VARINT_ZSTD:
        if codec is None:
            raise ValueError("encoding 2 blob but no codec loaded")
        return codec.decode(blob)
    raise ValueError(f"unknown page_tokens encoding {encoding}")


def load_codec(conn: sqlite3.Connection, level: int = DEFAULT_ZSTD_LEVEL) -> Optional[BlobCodecV2]:
    """Load the encoding-2 codec from token_codec, or None if absent."""
    if not _table_exists(conn, "token_codec"):
        return None
    import numpy as np

    cur = conn.cursor()
    cur.execute("SELECT data, meta FROM token_codec WHERE key='rank_to_def'")
    row = cur.fetchone()
    if not row:
        return None
    rank_to_def = np.frombuffer(row[0], dtype="<u4")
    cur.execute("SELECT data FROM token_codec WHERE key='zstd_dict'")
    drow = cur.fetchone()
    if not drow:
        return None
    meta = json.loads(row[1]) if row[1] else {}
    return BlobCodecV2(rank_to_def, bytes(drow[0]), meta.get("level", level))


def _page_tokens_encoding_counts(conn: sqlite3.Connection) -> dict[int, int]:
    cur = conn.cursor()
    if not _page_tokens_has_encoding_column(conn):
        cur.execute("SELECT COUNT(*) FROM page_tokens")
        return {ENCODING_RAW_U32: cur.fetchone()[0]}
    cur.execute("SELECT encoding, COUNT(*) FROM page_tokens GROUP BY encoding")
    return {int(r[0]): int(r[1]) for r in cur.fetchall()}


def _iter_page_rows(read_conn: sqlite3.Connection, chunk: int):
    """Stream (book_id, part_index, page_id, encoding, token_ids) rows."""
    cur = read_conn.cursor()
    if _page_tokens_has_encoding_column(read_conn):
        cur.execute(
            "SELECT book_id, part_index, page_id, encoding, token_ids FROM page_tokens"
        )
    else:
        cur.execute(
            f"SELECT book_id, part_index, page_id, {ENCODING_RAW_U32}, token_ids "
            "FROM page_tokens"
        )
    while True:
        rows = cur.fetchmany(chunk)
        if not rows:
            return
        yield rows


def recompress_page_tokens(
    level: int = DEFAULT_ZSTD_LEVEL,
    dict_size: int = DEFAULT_DICT_SIZE,
    dict_samples: int = DEFAULT_DICT_SAMPLES,
    retrain: bool = False,
    verify_sample: int = DEFAULT_VERIFY_SAMPLE,
    fixtures_dir: Optional[Path] = None,
    vacuum: bool = True,
    seed: int = 12345,
) -> None:
    """Re-encode every encoding-1 page blob as encoding 2."""
    import random

    import numpy as np
    import zstandard as zstd

    sys.stdout.reconfigure(line_buffering=True)
    rng = random.Random(seed)

    if not CORPUS_DB_PATH.exists():
        raise SystemExit(f"corpus.db not found at {CORPUS_DB_PATH}")

    size_before = CORPUS_DB_PATH.stat().st_size
    print("=" * 70)
    print("RECOMPRESS page_tokens  (encoding 1 -> 2)")
    print("=" * 70)
    print(f"corpus.db:     {CORPUS_DB_PATH} ({size_before / 1e9:.3f} GB)")
    print(f"zstd level:    {level}")
    print(f"dict size:     {dict_size:,} bytes from up to {dict_samples:,} sample pages")
    print(f"retrain codec: {retrain}")
    print()

    conn = connect_db(CORPUS_DB_PATH, large_cache=True)
    read_conn = connect_db(CORPUS_DB_PATH, large_cache=False)

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS token_codec (
            key TEXT PRIMARY KEY, data BLOB NOT NULL, meta TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_token_def_lemma ON token_definitions(lemma_id);
        CREATE INDEX IF NOT EXISTS idx_token_def_root  ON token_definitions(root_id);
        DROP INDEX IF EXISTS idx_token_def_surface;
        """
    )
    conn.commit()

    enc_counts = _page_tokens_encoding_counts(conn)
    n_raw = enc_counts.get(ENCODING_RAW_U32, 0)
    n_v2 = enc_counts.get(ENCODING_RANK_VARINT_ZSTD, 0)
    total_rows = sum(enc_counts.values())
    print(f"page_tokens rows: {total_rows:,}  (encoding 1: {n_raw:,}, encoding 2: {n_v2:,})")
    if n_raw == 0:
        print("Nothing to recompress.")
        conn.close()
        read_conn.close()
        return

    cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM token_definitions")
    max_def = int(cur.fetchone()[0] or 0)

    print("\nPass 1/2: counting token frequencies and sampling pages...")
    t0 = time.time()
    counts = np.zeros(max_def + 1, dtype=np.int64)
    reservoir: list[bytes] = []
    seen_raw = 0
    done = 0
    for rows in _iter_page_rows(read_conn, RECOMPRESS_CHUNK_PAGES):
        raw = [r[4] for r in rows if r[3] == ENCODING_RAW_U32]
        if raw:
            ids = np.frombuffer(b"".join(raw), dtype="<u4")
            if len(ids):
                if int(ids.max()) > max_def:
                    raise SystemExit(
                        f"page_tokens references token id {int(ids.max())} > "
                        f"MAX(token_definitions.id)={max_def}; database is inconsistent"
                    )
                counts += np.bincount(ids, minlength=max_def + 1)
            for b in raw:
                seen_raw += 1
                if len(reservoir) < dict_samples:
                    reservoir.append(bytes(b))
                else:
                    j = rng.randrange(seen_raw)
                    if j < dict_samples:
                        reservoir[j] = bytes(b)
        done += len(rows)
        if done % (RECOMPRESS_CHUNK_PAGES * 10) == 0:
            print(f"  {done:,}/{total_rows:,} rows  ({time.time() - t0:.0f}s)")
    total_tokens_raw = int(counts.sum())
    print(f"  {n_raw:,} raw pages, {total_tokens_raw:,} tokens, "
          f"{int((counts > 0).sum()):,} distinct definitions used  ({time.time() - t0:.0f}s)")

    existing = None if retrain else load_codec(conn, level)
    if existing is not None:
        old_map = existing.rank_to_def
        print(f"\nReusing stored rank map ({len(old_map):,} entries) and dictionary")
        known = np.zeros(max_def + 1, dtype=bool)
        known[old_map] = True
        new_ids = np.nonzero(~known)[0]
        new_ids = new_ids[new_ids >= 1]
        if len(new_ids):
            order = np.argsort(-counts[new_ids], kind="stable")
            rank_to_def = np.concatenate([old_map, new_ids[order].astype(np.uint32)])
            print(f"  Appended {len(new_ids):,} new definitions to the rank map")
        else:
            rank_to_def = old_map
        dict_bytes = existing.dict_bytes
        codec_meta_extra = {"extended_from": len(old_map)}
    else:
        print("\nBuilding rank map (rank 0 = most frequent definition)...")
        ids = np.arange(1, max_def + 1, dtype=np.int64)
        order = np.argsort(-counts[1:], kind="stable")
        rank_to_def = ids[order].astype(np.uint32)
        top = rank_to_def[:5].tolist()
        print(f"  {len(rank_to_def):,} definitions ranked; top ids {top} "
              f"with counts {[int(counts[i]) for i in top]}")

        print(f"\nTraining zstd dictionary on {len(reservoir):,} sample pages...")
        def_to_rank = np.zeros(max_def + 1, dtype=np.uint32)
        def_to_rank[rank_to_def] = np.arange(len(rank_to_def), dtype=np.uint32)
        payloads, _ = rank_varint_payloads(reservoir, def_to_rank)
        payloads = [p for p in payloads if p]
        t1 = time.time()
        try:
            dict_bytes = zstd.train_dictionary(dict_size, payloads, level=level).as_bytes()
        except zstd.ZstdError:
            if len(payloads) > 1000:
                raise
            # Too little text to train on: the samples serve as the dictionary.
            print("  too few pages to train a dictionary; using the pages as one")
            dict_bytes = (b"\0" * 64 + b"".join(payloads))[:dict_size]
        print(f"  dictionary: {len(dict_bytes):,} bytes  ({time.time() - t1:.1f}s)")
        codec_meta_extra = {}

    codec = BlobCodecV2(rank_to_def, dict_bytes, level)

    meta = {
        "level": level,
        "dict_size": len(dict_bytes),
        "dict_samples": len(reservoir),
        "definitions": int(len(rank_to_def)),
        "tokens_counted": total_tokens_raw,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **codec_meta_extra,
    }
    cur.execute(
        "INSERT OR REPLACE INTO token_codec (key, data, meta) VALUES ('rank_to_def', ?, ?)",
        (sqlite3.Binary(np.asarray(rank_to_def, dtype="<u4").tobytes()), json.dumps(meta)),
    )
    cur.execute(
        "INSERT OR REPLACE INTO token_codec (key, data, meta) VALUES ('zstd_dict', ?, ?)",
        (sqlite3.Binary(dict_bytes), json.dumps({"level": level, "size": len(dict_bytes)})),
    )
    conn.commit()

    print("\nPass 2/2: encoding pages...")
    cur.execute("DROP TABLE IF EXISTS page_tokens_new")
    cur.execute(
        """
        CREATE TABLE page_tokens_new (
            book_id     INTEGER NOT NULL,
            part_index  INTEGER NOT NULL,
            page_id     INTEGER NOT NULL,
            encoding    INTEGER NOT NULL DEFAULT 1,
            token_ids   BLOB NOT NULL,
            PRIMARY KEY (book_id, part_index, page_id)
        ) WITHOUT ROWID
        """
    )
    conn.commit()

    t2 = time.time()
    done = 0
    bytes_in = 0
    bytes_out = 0
    verify_keys: list[tuple[int, int, int]] = []
    seen_for_verify = 0
    insert_sql = (
        "INSERT INTO page_tokens_new (book_id, part_index, page_id, encoding, token_ids) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    for rows in _iter_page_rows(read_conn, RECOMPRESS_CHUNK_PAGES):
        raw_rows = [r for r in rows if r[3] == ENCODING_RAW_U32]
        other_rows = [r for r in rows if r[3] != ENCODING_RAW_U32]
        out_rows = []
        if raw_rows:
            encoded = codec.encode_batch([r[4] for r in raw_rows])
            for r, e in zip(raw_rows, encoded):
                bytes_in += len(r[4])
                bytes_out += len(e)
                out_rows.append((r[0], r[1], r[2], ENCODING_RANK_VARINT_ZSTD, sqlite3.Binary(e)))
                seen_for_verify += 1
                if len(verify_keys) < verify_sample:
                    verify_keys.append((r[0], r[1], r[2]))
                else:
                    j = rng.randrange(seen_for_verify)
                    if j < verify_sample:
                        verify_keys[j] = (r[0], r[1], r[2])
        for r in other_rows:
            out_rows.append((r[0], r[1], r[2], r[3], sqlite3.Binary(r[4])))
        cur.executemany(insert_sql, out_rows)
        conn.commit()
        done += len(rows)
        if done % (RECOMPRESS_CHUNK_PAGES * 10) == 0:
            ratio = bytes_in / bytes_out if bytes_out else 0
            print(f"  {done:,}/{total_rows:,} rows  raw {bytes_in / 1e9:.3f} GB -> "
                  f"{bytes_out / 1e9:.3f} GB ({ratio:.2f}x)  ({time.time() - t2:.0f}s)")
    ratio = bytes_in / bytes_out if bytes_out else 0
    print(f"  done: raw {bytes_in / 1e9:.3f} GB -> {bytes_out / 1e9:.3f} GB "
          f"({ratio:.2f}x)  ({time.time() - t2:.0f}s)")

    print(f"\nVerifying {len(verify_keys):,} random pages decode to the original ids...")
    mismatches = 0
    old_cur = read_conn.cursor()
    has_enc_old = _page_tokens_has_encoding_column(read_conn)
    for (b, p, g) in verify_keys:
        if has_enc_old:
            old_cur.execute(
                "SELECT encoding, token_ids FROM page_tokens "
                "WHERE book_id=? AND part_index=? AND page_id=?", (b, p, g))
        else:
            old_cur.execute(
                f"SELECT {ENCODING_RAW_U32}, token_ids FROM page_tokens "
                "WHERE book_id=? AND part_index=? AND page_id=?", (b, p, g))
        old_enc, old_blob = old_cur.fetchone()
        cur.execute(
            "SELECT encoding, token_ids FROM page_tokens_new "
            "WHERE book_id=? AND part_index=? AND page_id=?", (b, p, g))
        new_enc, new_blob = cur.fetchone()
        if decode_blob(old_enc, old_blob, codec) != decode_blob(new_enc, new_blob, codec):
            mismatches += 1
            if mismatches <= 5:
                print(f"  MISMATCH at book={b} part={p} page={g}")
    if mismatches:
        conn.execute("DROP TABLE page_tokens_new")
        conn.commit()
        raise SystemExit(f"ABORT: {mismatches} pages failed round-trip; page_tokens left untouched")
    print("  OK")

    print("\nSwapping tables...")
    read_conn.close()
    cur.execute("DROP TABLE page_tokens")
    cur.execute("ALTER TABLE page_tokens_new RENAME TO page_tokens")
    cur.execute("UPDATE db_info SET schema_version = ?", (corpus_schema_version(conn),))
    conn.commit()

    if fixtures_dir is not None:
        export_fixtures(conn, codec, Path(fixtures_dir), n_pages=200, rng=rng)

    if vacuum:
        print("\nVACUUM (this rewrites the whole file; needs ~2x free disk)...")
        t3 = time.time()
        conn.execute("VACUUM")
        print(f"  done ({time.time() - t3:.0f}s)")
    conn.close()

    size_after = CORPUS_DB_PATH.stat().st_size
    print("\n" + "=" * 70)
    print("RECOMPRESS COMPLETE")
    print("=" * 70)
    print(f"corpus.db: {size_before / 1e9:.3f} GB -> {size_after / 1e9:.3f} GB")
    print(f"page blobs: {bytes_in / 1e9:.3f} GB -> {bytes_out / 1e9:.3f} GB ({ratio:.2f}x)")
    print(f"db_info.schema_version = {SCHEMA_VERSION}")
    if not vacuum:
        print("NOTE: --no-vacuum given; run VACUUM to reclaim the freed pages")
    print("=" * 70)


TRIPLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS triples (
    id        INTEGER PRIMARY KEY,
    surface   TEXT    NOT NULL,
    lemma_id  INTEGER NOT NULL,
    root_id   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_triples_lemma   ON triples(lemma_id);
CREATE INDEX IF NOT EXISTS idx_triples_root    ON triples(root_id);
CREATE INDEX IF NOT EXISTS idx_triples_surface ON triples(surface);
"""


def build_triples(vacuum: bool = False) -> None:
    """Populate `triples` and `token_definitions.triple_id` (schema v4)."""
    import numpy as np

    sys.stdout.reconfigure(line_buffering=True)
    if not CORPUS_DB_PATH.exists():
        raise SystemExit(f"corpus.db not found at {CORPUS_DB_PATH}")
    conn = connect_db(CORPUS_DB_PATH, large_cache=True)
    cur = conn.cursor()
    print("=" * 70)
    print("BUILD TRIPLES  (surface, lemma_id, root_id)")
    print("=" * 70)

    t0 = time.time()
    cur.execute("SELECT id, surface, lemma_id, root_id FROM token_definitions")
    rows = cur.fetchall()
    max_def = max((r[0] for r in rows), default=0)
    print(f"  {len(rows):,} token definitions read ({time.time() - t0:.1f}s)")

    codec = load_codec(conn)
    def_rank = np.full(max_def + 1, np.iinfo(np.int64).max, dtype=np.int64)
    if codec is not None:
        def_rank[codec.rank_to_def] = np.arange(len(codec.rank_to_def), dtype=np.int64)
        print(f"  using rank map with {len(codec.rank_to_def):,} entries for triple ordering")
    else:
        print("  no rank map (run --recompress first for frequency-ordered triple ids)")

    key_to_best: dict[tuple, int] = {}
    key_of_def: dict[int, tuple] = {}
    for def_id, surface, lemma_id, root_id in rows:
        key = (surface, lemma_id, root_id)
        key_of_def[def_id] = key
        r = int(def_rank[def_id])
        prev = key_to_best.get(key)
        if prev is None or r < prev:
            key_to_best[key] = r
    ordered = sorted(key_to_best.items(), key=lambda kv: (kv[1], kv[0][1], kv[0][0]))
    triple_id_of_key = {key: i + 1 for i, (key, _) in enumerate(ordered)}
    print(f"  {len(ordered):,} distinct triples ({time.time() - t0:.1f}s)")

    cur.execute("DROP TABLE IF EXISTS triples")
    conn.commit()
    conn.executescript(TRIPLES_SCHEMA)
    cur.executemany(
        "INSERT INTO triples (id, surface, lemma_id, root_id) VALUES (?, ?, ?, ?)",
        ((triple_id_of_key[key], key[0], key[1], key[2]) for key, _ in ordered),
    )
    conn.commit()
    print(f"  triples written ({time.time() - t0:.1f}s)")

    cur.execute("PRAGMA table_info(token_definitions)")
    if not any(r[1] == "triple_id" for r in cur.fetchall()):
        cur.execute("ALTER TABLE token_definitions ADD COLUMN triple_id INTEGER")
    cur.executemany(
        "UPDATE token_definitions SET triple_id = ? WHERE id = ?",
        ((triple_id_of_key[key_of_def[d]], d) for d in key_of_def),
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_token_def_triple ON token_definitions(triple_id)")
    cur.execute("UPDATE db_info SET schema_version = ?", (corpus_schema_version(conn),))
    conn.commit()
    print(f"  token_definitions.triple_id populated ({time.time() - t0:.1f}s)")

    cur.execute("SELECT COUNT(*) FROM token_definitions WHERE triple_id IS NULL")
    nulls = cur.fetchone()[0]
    if nulls:
        raise SystemExit(f"ABORT: {nulls} token_definitions without triple_id")

    if vacuum:
        print("  VACUUM...")
        conn.execute("VACUUM")
    conn.close()
    print("=" * 70)
    print(f"TRIPLES COMPLETE: {len(ordered):,} triples for {len(rows):,} definitions; "
          f"db_info.schema_version = 4")
    print("=" * 70)


def export_fixtures(conn: sqlite3.Connection, codec: "BlobCodecV2", out_dir: Path,
                    n_pages: int, rng) -> None:
    """Write decoder fixtures for the Rust side."""
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT book_id, part_index, page_id, token_ids FROM page_tokens "
        f"WHERE encoding = {ENCODING_RANK_VARINT_ZSTD} ORDER BY RANDOM() LIMIT ?",
        (n_pages,),
    )
    rows = cur.fetchall()
    with open(out_dir / "blobs_v2.jsonl", "w", encoding="utf-8") as f:
        for b, p, g, blob in rows:
            def_ids = codec.decode(blob)
            ranks = [int(codec.def_to_rank[d]) for d in def_ids]
            f.write(json.dumps({
                "book_id": b, "part_index": p, "page_id": g,
                "n_tokens": len(def_ids), "def_ids": def_ids, "ranks": ranks,
                "blob_hex": bytes(blob).hex(),
            }) + "\n")
    (out_dir / "zstd_dict.bin").write_bytes(codec.dict_bytes)
    (out_dir / "rank_to_def.bin").write_bytes(
        np.asarray(codec.rank_to_def, dtype="<u4").tobytes())
    (out_dir / "README.txt").write_text(
        "Kashshaf page_tokens encoding 2 fixtures\n"
        "=========================================\n"
        "blob = varint(n_tokens) || zstd_frame\n"
        "  varint: unsigned LEB128\n"
        "  zstd_frame: standard zstd frame (magic present), no checksum, no content\n"
        "              size, no dictionary id; compressed with zstd_dict.bin\n"
        "  decompressed payload: n_tokens unsigned LEB128 varints, each a rank\n"
        "  token_definitions.id = rank_to_def[rank]  (rank_to_def.bin, uint32 LE)\n"
        f"zstd level used: {codec.level}\n",
        encoding="utf-8",
    )
    print(f"  wrote {len(rows)} fixture pages to {out_dir}")


def init_metadata_schema(conn: sqlite3.Connection):
    """Initialize metadata.db schema (authors, genres, books)."""
    conn.executescript(METADATA_SCHEMA)
    conn.commit()
    _ensure_parts_column(conn)


class DatabaseWriter:
    """All database writes, in the main process: SQLite allows one writer."""

    def __init__(self, corpus_conn: sqlite3.Connection, metadata_conn: sqlite3.Connection):
        self.corpus_conn = corpus_conn
        self.metadata_conn = metadata_conn
        self.cursor = corpus_conn.cursor()
        self.meta_cursor = metadata_conn.cursor()

        self.lemma_cache: dict[str, int] = {}
        self.root_cache: dict[str, int] = {}
        self.pos_cache: dict[str, int] = {}
        self.feat_cache: dict[str, int] = {}
        self.clit_cache: dict[str, int] = {}
        self.token_cache: dict[tuple, int] = {}

        self.empty_features = "[]"
        self.empty_clitics = "[]"

        self.books_written = 0
        self.pages_written = 0
        self.tokens_written = 0

        self.page_tokens_batch = []

    def preload_caches(self):
        """Load existing data into caches for incremental builds."""
        print("Pre-loading caches from existing database...")

        self.cursor.execute("SELECT lemma, id FROM lemmas")
        self.lemma_cache = {row[0]: row[1] for row in self.cursor.fetchall()}
        print(f"  Lemmas: {len(self.lemma_cache):,}")

        self.cursor.execute("SELECT root, id FROM roots")
        self.root_cache = {row[0]: row[1] for row in self.cursor.fetchall()}
        print(f"  Roots: {len(self.root_cache):,}")

        self.cursor.execute("SELECT pos, id FROM pos_types")
        self.pos_cache = {row[0]: row[1] for row in self.cursor.fetchall()}
        print(f"  POS types: {len(self.pos_cache):,}")

        self.cursor.execute("SELECT features, id FROM feature_sets")
        self.feat_cache = {row[0]: row[1] for row in self.cursor.fetchall()}
        print(f"  Feature sets: {len(self.feat_cache):,}")

        self.cursor.execute("SELECT clitics, id FROM clitic_sets")
        self.clit_cache = {row[0]: row[1] for row in self.cursor.fetchall()}
        print(f"  Clitic sets: {len(self.clit_cache):,}")

        self.cursor.execute("""
            SELECT id, surface, lemma_id, root_id, pos_id, feature_set_id, clitic_set_id
            FROM token_definitions
        """)
        for row in self.cursor.fetchall():
            key = (row[1], row[2], row[3], row[4], row[5], row[6])
            self.token_cache[key] = row[0]
        print(f"  Token definitions: {len(self.token_cache):,}")
        print()

        self._get_or_create_cached(self.feat_cache, "feature_sets", "features", self.empty_features)
        self._get_or_create_cached(self.clit_cache, "clitic_sets", "clitics", self.empty_clitics)

    def _get_or_create_cached(self, cache: dict, table: str, column: str, value) -> int:
        """Get or create an ID for a lookup table value."""
        if value in cache:
            return cache[value]

        self.cursor.execute(
            f"INSERT INTO {table} ({column}) VALUES (?) "
            f"ON CONFLICT({column}) DO UPDATE SET {column}={column} "
            f"RETURNING id",
            (value,)
        )
        id_ = self.cursor.fetchone()[0]
        cache[value] = id_
        return id_

    def _get_token_def_id(self, surface, lemma_id, root_id, pos_id, feat_id, clit_id) -> int:
        """Get or create token definition ID."""
        key = (surface, lemma_id, root_id, pos_id, feat_id, clit_id)

        if key in self.token_cache:
            return self.token_cache[key]

        self.cursor.execute("""
            INSERT INTO token_definitions
                (surface, lemma_id, root_id, pos_id, feature_set_id, clitic_set_id)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(surface, lemma_id, root_id, pos_id, feature_set_id, clitic_set_id)
            DO UPDATE SET surface=surface
            RETURNING id
        """, (surface, lemma_id, root_id, pos_id, feat_id, clit_id))

        id_ = self.cursor.fetchone()[0]
        self.token_cache[key] = id_
        return id_

    def _flush_batches(self):
        """Write batched page_tokens to corpus.db."""
        if self.page_tokens_batch:
            self.cursor.executemany(
                "INSERT INTO page_tokens "
                "(book_id, part_index, page_id, encoding, token_ids) "
                f"VALUES (?, ?, ?, {ENCODING_RAW_U32}, ?)",
                self.page_tokens_batch
            )
            self.page_tokens_batch.clear()

    def delete_book_tokens(self, book_id: int):
        """Remove a book's token data from corpus.db (metadata stays in metadata.db)."""
        self.cursor.execute("DELETE FROM page_tokens WHERE book_id = ?", (book_id,))
        self.corpus_conn.commit()

    def write_book(self, book: ProcessedBook) -> tuple[int, int, int]:
        """Write a book's tokens to corpus.db and return (page_count, token_count, parts)."""
        page_count = 0
        token_count = 0
        seen_parts: set = set()

        for page in book.pages:
            token_ids = array("I")

            for surface, lemma, root, pos, feats_json, clits_json in page.tokens:
                lemma_id = self._get_or_create_cached(
                    self.lemma_cache, "lemmas", "lemma", lemma
                )
                root_id = (
                    self._get_or_create_cached(self.root_cache, "roots", "root", root)
                    if root else None
                )
                pos_id = self._get_or_create_cached(
                    self.pos_cache, "pos_types", "pos", pos
                )
                feat_id = self._get_or_create_cached(
                    self.feat_cache, "feature_sets", "features", feats_json
                )
                clit_id = self._get_or_create_cached(
                    self.clit_cache, "clitic_sets", "clitics", clits_json
                )

                token_def_id = self._get_token_def_id(
                    surface, lemma_id, root_id, pos_id, feat_id, clit_id
                )
                token_ids.append(token_def_id)

            self.page_tokens_batch.append((
                page.book_id,
                page.part_index,
                page.page_id,
                token_ids.tobytes(),
            ))

            page_count += 1
            token_count += len(token_ids)
            seen_parts.add(page.part_index)
            self.pages_written += 1
            self.tokens_written += len(token_ids)

            if len(self.page_tokens_batch) >= PAGE_BATCH_SIZE:
                self._flush_batches()
                self.corpus_conn.commit()

            if self.pages_written % COMMIT_INTERVAL == 0:
                self._flush_batches()
                self.corpus_conn.commit()

        self._flush_batches()
        self.corpus_conn.commit()

        parts_count = len(seen_parts)

        self.meta_cursor.execute(
            "UPDATE books SET page_count = ?, token_count = ?, parts = ? WHERE id = ?",
            (page_count, token_count, parts_count, book.book_id)
        )
        self.metadata_conn.commit()
        self.books_written += 1

        return page_count, token_count, parts_count


def update_token_counts(
    corpus_conn: sqlite3.Connection,
    metadata_conn: sqlite3.Connection,
    zero_missing: bool = False,
    corpus_version_override: Optional[str] = None,
) -> int:
    """Update page_count, token_count and parts in metadata.db from corpus.db's blobs."""
    from collections import defaultdict
    try:
        from tqdm import tqdm
        have_tqdm = True
    except ImportError:
        have_tqdm = False

    corpus_cursor = corpus_conn.cursor()
    meta_cursor = metadata_conn.cursor()

    corpus_cursor.execute("SELECT COUNT(*) FROM page_tokens")
    total_pages = corpus_cursor.fetchone()[0]

    print(f"Counting tokens from {total_pages:,} page_tokens rows...")

    has_encoding = _page_tokens_has_encoding_column(corpus_conn)
    if has_encoding:
        corpus_cursor.execute(
            "SELECT book_id, part_index, encoding, token_ids FROM page_tokens"
        )
    else:
        corpus_cursor.execute(
            f"SELECT book_id, part_index, {ENCODING_RAW_U32}, token_ids FROM page_tokens"
        )

    token_counts: dict = defaultdict(int)
    page_counts: dict = defaultdict(int)
    parts_seen: dict = defaultdict(set)
    iterator = (
        tqdm(corpus_cursor, total=total_pages, desc="Counting")
        if have_tqdm else corpus_cursor
    )
    for row in iterator:
        book_id, part_index, encoding, blob = row
        num_tokens = blob_token_count(encoding, blob)
        token_counts[book_id] += num_tokens
        page_counts[book_id] += 1
        parts_seen[book_id].add(part_index)

    parts_counts = {bid: len(parts) for bid, parts in parts_seen.items()}

    if zero_missing:
        meta_cursor.execute("SELECT id FROM books")
        all_book_ids = {row[0] for row in meta_cursor.fetchall()}
        books_with_data = set(token_counts.keys())
        books_missing = all_book_ids - books_with_data

        for bid in books_missing:
            token_counts[bid] = 0
            page_counts[bid] = 0
            parts_counts[bid] = 0
        print(f"Zeroing counts for {len(books_missing):,} books with no "
              f"page_tokens entries...")

    print(f"Updating counts for {len(token_counts):,} books in metadata.db...")
    updates = [
        (page_counts[bid], token_counts[bid], parts_counts[bid], bid)
        for bid in token_counts.keys()
    ]
    meta_cursor.executemany(
        "UPDATE books SET page_count = ?, token_count = ?, parts = ? WHERE id = ?",
        updates
    )
    metadata_conn.commit()

    total_tokens = sum(token_counts.values())
    total_pages_counted = sum(page_counts.values())
    total_parts = sum(parts_counts.values())
    print(f"Total pages counted:  {total_pages_counted:,}")
    print(f"Total tokens counted: {total_tokens:,}")
    print(f"Total parts counted:  {total_parts:,}")

    existing_meta = _read_existing_db_info(metadata_conn)
    existing_corpus = _read_existing_db_info(corpus_conn)
    if corpus_version_override is not None:
        version_str = corpus_version_override
    elif existing_meta is not None:
        version_str = existing_meta[0]
    elif existing_corpus is not None:
        version_str = existing_corpus[0]
    else:
        version_str = "unknown"

    meta_cursor.execute(
        "SELECT COUNT(*), "
        "       COALESCE(SUM(page_count), 0), "
        "       COALESCE(SUM(token_count), 0) "
        "FROM books WHERE in_corpus = 1"
    )
    in_corpus_books, in_corpus_pages, in_corpus_tokens = meta_cursor.fetchone()
    _write_db_info(
        metadata_conn,
        corpus_version=version_str,
        schema_version=METADATA_SCHEMA_VERSION,
        book_count=int(in_corpus_books),
        page_count=int(in_corpus_pages),
        token_count=int(in_corpus_tokens),
    )
    print(f"Refreshed metadata.db.db_info: "
          f"books={in_corpus_books:,}, pages={in_corpus_pages:,}, "
          f"tokens={in_corpus_tokens:,}")

    corpus_cursor.execute("SELECT COUNT(DISTINCT book_id) FROM page_tokens")
    corpus_book_count = corpus_cursor.fetchone()[0]
    if KEEP_CORPUS_DB:
        print("corpus.db.db_info left as it is (--keep-corpus-db)")
        return total_tokens
    _write_db_info(
        corpus_conn,
        corpus_version=version_str,
        schema_version=corpus_schema_version(corpus_conn),
        book_count=int(corpus_book_count),
        page_count=int(total_pages_counted),
        token_count=int(total_tokens),
    )
    print(f"Refreshed corpus.db.db_info:   "
          f"books={corpus_book_count:,}, pages={total_pages_counted:,}, "
          f"tokens={total_tokens:,}")

    return total_tokens


def update_counts_only():
    """Standalone --update-counts: refresh the counts and db_info in both databases."""
    if not CORPUS_DB_PATH.exists():
        raise SystemExit(f"corpus.db not found at {CORPUS_DB_PATH} — "
                         f"nothing to count from")
    if not METADATA_DB_PATH.exists():
        raise SystemExit(f"metadata.db not found at {METADATA_DB_PATH} — "
                         f"run --load-metadata first")

    print("=" * 70)
    print("UPDATE COUNTS")
    print("=" * 70)
    print(f"corpus.db:   {CORPUS_DB_PATH}")
    print(f"metadata.db: {METADATA_DB_PATH}")
    print()

    corpus_conn = open_corpus_for_counts()
    metadata_conn = connect_db(METADATA_DB_PATH, large_cache=False)

    _ensure_parts_column(metadata_conn)
    update_token_counts(corpus_conn, metadata_conn, zero_missing=True)

    meta_cur = metadata_conn.cursor()
    meta_cur.execute(
        "SELECT COUNT(*), SUM(page_count), SUM(token_count), SUM(parts) "
        "FROM books WHERE in_corpus = 1"
    )
    n, p, t, parts = meta_cur.fetchone()
    print()
    print(f"in_corpus=1 totals after update:")
    print(f"  Books:  {n or 0:,}")
    print(f"  Pages:  {p or 0:,}")
    print(f"  Tokens: {t or 0:,}")
    print(f"  Parts:  {parts or 0:,}")

    corpus_conn.close()
    metadata_conn.close()
    print("=" * 70)


def get_existing_book_ids(meta_conn: sqlite3.Connection) -> set[int]:
    """Get set of ALL book IDs from metadata.db, regardless of in_corpus."""
    cursor = meta_conn.cursor()
    cursor.execute("SELECT id FROM books")
    return {row[0] for row in cursor.fetchall()}


def get_in_corpus_book_ids(meta_conn: sqlite3.Connection) -> set[int]:
    """Get set of book IDs from metadata.db where in_corpus = 1."""
    cursor = meta_conn.cursor()
    cursor.execute("SELECT id FROM books WHERE in_corpus = 1")
    return {row[0] for row in cursor.fetchall()}


def get_books_with_tokens(corpus_conn: sqlite3.Connection) -> set[int]:
    """Get set of book IDs that have token data in corpus.db."""
    cursor = corpus_conn.cursor()
    cursor.execute("SELECT DISTINCT book_id FROM page_tokens")
    return {row[0] for row in cursor.fetchall()}


def build_database(
    rebuild: bool = False,
    book_ids: Optional[list[int]] = None,
    force: bool = False,
    version: Optional[str] = None,
    num_workers: Optional[int] = None
):
    """Build or update the databases using multiple worker processes."""
    sys.stdout.reconfigure(line_buffering=True)

    if num_workers is None:
        num_workers = DEFAULT_NUM_WORKERS
    num_workers = max(1, num_workers)

    if rebuild and CORPUS_DB_PATH.exists():
        print(f"Removing existing corpus database: {CORPUS_DB_PATH}")
        CORPUS_DB_PATH.unlink()

    if not BOOKS_DIR.exists():
        raise SystemExit(f"Books directory missing: {BOOKS_DIR}")

    all_files = {int(p.stem): p for p in BOOKS_DIR.glob("*.jsonl")}
    print(f"Found {len(all_files):,} book JSONL files in {BOOKS_DIR}")
    print(f"Using {num_workers} worker processes\n")

    corpus_conn = connect_db(CORPUS_DB_PATH, large_cache=True)
    init_corpus_schema(corpus_conn)

    metadata_conn = connect_db(METADATA_DB_PATH, large_cache=False)
    init_metadata_schema(metadata_conn)

    meta_cursor = metadata_conn.cursor()
    meta_cursor.execute("SELECT COUNT(*) FROM books")
    if rebuild or meta_cursor.fetchone()[0] == 0:
        load_metadata(metadata_conn, version)

    existing_token_ids = get_books_with_tokens(corpus_conn) if not rebuild else set()
    all_book_ids = get_existing_book_ids(metadata_conn)
    in_corpus_ids = get_in_corpus_book_ids(metadata_conn)

    excluded_count = len(all_book_ids) - len(in_corpus_ids)
    if excluded_count > 0:
        print(f"Books in metadata: {len(all_book_ids):,} total "
              f"({len(in_corpus_ids):,} in_corpus=1, "
              f"{excluded_count:,} in_corpus=0 — excluded from corpus.db)\n")

    if book_ids is not None:
        missing = set(book_ids) - set(all_files.keys())
        if missing:
            raise SystemExit(f"Book files not found: {sorted(missing)}")

        not_in_metadata = set(book_ids) - all_book_ids
        if not_in_metadata:
            raise SystemExit(
                f"Books not in metadata.db: {sorted(not_in_metadata)}\n"
                f"Run --load-metadata first to add them"
            )

        not_in_corpus = set(book_ids) - in_corpus_ids
        if not_in_corpus:
            raise SystemExit(
                f"Books flagged in_corpus=0 in metadata.db: "
                f"{sorted(not_in_corpus)}\n"
                f"These books are excluded from corpus.db by design.\n"
                f"To include them, set in_corpus=1 in metadata-master.xlsx "
                f"and re-run --load-metadata."
            )

        if not force:
            already_have_tokens = set(book_ids) & existing_token_ids
            if already_have_tokens:
                raise SystemExit(
                    f"Books already have token data: {sorted(already_have_tokens)}\n"
                    f"Use --force to replace them"
                )

        files_to_process = [(bid, all_files[bid]) for bid in sorted(book_ids)]
    else:
        new_ids = (in_corpus_ids & set(all_files.keys())) - existing_token_ids
        files_to_process = [(bid, all_files[bid]) for bid in sorted(new_ids)]

    in_corpus_without_jsonl = in_corpus_ids - set(all_files.keys())
    if in_corpus_without_jsonl:
        sample = sorted(in_corpus_without_jsonl)[:10]
        print(f"NOTE: {len(in_corpus_without_jsonl):,} in_corpus=1 books "
              f"have no JSONL file (sample: {sample})\n")

    jsonls_excluded_by_in_corpus = (
        set(all_files.keys()) - in_corpus_ids
    ) & all_book_ids
    if jsonls_excluded_by_in_corpus:
        print(f"NOTE: {len(jsonls_excluded_by_in_corpus):,} JSONL files "
              f"exist for in_corpus=0 books — these will NOT be added to "
              f"corpus.db (sample: {sorted(jsonls_excluded_by_in_corpus)[:10]})\n")

    if not files_to_process:
        print("No new books to process.")
        print("\nRefreshing token counts from existing page_tokens blobs...")
        update_token_counts(corpus_conn, metadata_conn, zero_missing=True,
                            corpus_version_override=version)
        corpus_conn.close()
        metadata_conn.close()
        return

    print(f"Books to process: {len(files_to_process):,}")
    if existing_token_ids and not rebuild:
        print(f"Books already with tokens: {len(existing_token_ids):,}")
    print()

    writer = DatabaseWriter(corpus_conn, metadata_conn)
    if existing_token_ids and not rebuild:
        writer.preload_caches()
    else:
        writer._get_or_create_cached(writer.feat_cache, "feature_sets", "features", writer.empty_features)
        writer._get_or_create_cached(writer.clit_cache, "clitic_sets", "clitics", writer.empty_clitics)

    task_queue: Queue = Queue(maxsize=TASK_QUEUE_SIZE)
    result_queue: Queue = Queue(maxsize=QUEUE_SIZE)

    workers = []
    for i in range(num_workers):
        p = Process(target=worker_process, args=(task_queue, result_queue, i))
        p.daemon = True
        p.start()
        workers.append(p)

    def feed_tasks():
        for bid, book_file in files_to_process:
            task_queue.put(str(book_file))
        for _ in range(num_workers):
            task_queue.put(DONE_SENTINEL)

    feeder_thread = threading.Thread(target=feed_tasks, daemon=True)
    feeder_thread.start()

    print("=" * 70)
    print("BEGIN PROCESSING")
    print("=" * 70 + "\n")

    start_time = time.time()
    last_report = start_time
    done_count = 0
    errors = []

    books_to_delete = set(book_ids) & existing_token_ids if force and book_ids else set()
    for bid in books_to_delete:
        print(f"Removing existing token data for book {bid}...")
        writer.delete_book_tokens(bid)

    while done_count < num_workers:
        try:
            result = result_queue.get(timeout=2)
        except Empty:
            alive = sum(1 for p in workers if p.is_alive())
            if alive == 0 and done_count < num_workers:
                print("Warning: All workers died unexpectedly", file=sys.stderr)
                break
            continue

        if result == DONE_SENTINEL:
            done_count += 1
            continue

        book: ProcessedBook = result

        if book.error:
            errors.append((book.book_id, book.error))
            print(f"[ERR] Book {book.book_id}: {book.error}", file=sys.stderr)
            continue

        try:
            page_count, token_count, parts_count = writer.write_book(book)
            print(
                f"[OK] Book {book.book_id}: {page_count:,} pages, "
                f"{token_count:,} tokens, {parts_count} parts",
                flush=True
            )
        except Exception as e:
            errors.append((book.book_id, str(e)))
            print(f"[ERR] Book {book.book_id} DB write error: {e}", file=sys.stderr)
            continue

        now = time.time()
        if now - last_report >= REPORT_INTERVAL:
            elapsed = now - start_time
            pages_per_sec = writer.pages_written / elapsed if elapsed > 0 else 0
            print(
                f"\n[{elapsed:8.1f}s] "
                f"Books: {writer.books_written:,} | "
                f"Pages: {writer.pages_written:,} | "
                f"Tokens: {writer.tokens_written:,} | "
                f"{pages_per_sec:,.0f} pages/s\n",
                flush=True
            )
            last_report = now

    feeder_thread.join(timeout=5)

    for p in workers:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()

    print("\n" + "=" * 70)
    print("UPDATING TOKEN COUNTS")
    print("=" * 70)
    update_token_counts(corpus_conn, metadata_conn, zero_missing=True,
                        corpus_version_override=version)

    elapsed = timedelta(seconds=int(time.time() - start_time))
    corpus_size = CORPUS_DB_PATH.stat().st_size / (1024 * 1024)
    metadata_size = METADATA_DB_PATH.stat().st_size / (1024 * 1024)

    cursor = corpus_conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM token_definitions")
    unique_tokens = cursor.fetchone()[0]

    meta_cursor = metadata_conn.cursor()
    meta_cursor.execute("SELECT COUNT(*) FROM books")
    total_books = meta_cursor.fetchone()[0]

    meta_cursor.execute("SELECT COUNT(*) FROM books WHERE in_corpus = 1")
    in_corpus_books = meta_cursor.fetchone()[0]

    meta_cursor.execute(
        "SELECT SUM(page_count), SUM(token_count), SUM(parts) "
        "FROM books WHERE in_corpus = 1"
    )
    total_pages, total_tokens, total_parts = meta_cursor.fetchone()
    total_pages = total_pages or 0
    total_tokens = total_tokens or 0
    total_parts = total_parts or 0

    print("\n" + "=" * 70)
    print("COMPLETE")
    print("=" * 70)
    print(f"Workers used: {num_workers}")
    print(f"Books processed this run: {writer.books_written:,}")
    print(f"Total books in metadata: {total_books:,} "
          f"({in_corpus_books:,} in_corpus=1, "
          f"{total_books - in_corpus_books:,} excluded)")
    print(f"Total pages (in_corpus=1):  {total_pages:,}")
    print(f"Total tokens (in_corpus=1): {total_tokens:,}")
    print(f"Total parts (in_corpus=1):  {total_parts:,}")
    print(f"Unique token definitions: {unique_tokens:,}")
    if unique_tokens > 0:
        print(f"Compression ratio: {total_tokens / unique_tokens:.1f}x")
    print(f"corpus.db size: {corpus_size:.2f} MB")
    print(f"metadata.db size: {metadata_size:.2f} MB")
    print(f"Time: {elapsed}")

    if errors:
        print(f"\nErrors ({len(errors)}):")
        for bid, err in errors[:10]:
            print(f"  Book {bid}: {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")

    if version:
        print(f"Version: {version} (schema v{SCHEMA_VERSION})")

    print("=" * 70)
    corpus_conn.close()
    metadata_conn.close()


def show_status():
    """Show database status and statistics."""
    print("=" * 70)
    print("DATABASE STATUS")
    print("=" * 70)

    if CORPUS_DB_PATH.exists():
        print(f"\n[corpus.db] {CORPUS_DB_PATH}")
        print(f"  Size: {CORPUS_DB_PATH.stat().st_size / (1024 * 1024):.2f} MB")

        corpus_conn = sqlite3.connect(CORPUS_DB_PATH)
        cur = corpus_conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='db_info'")
        if cur.fetchone():
            cur.execute("""
                SELECT corpus_version, schema_version, built_at,
                       book_count, page_count, token_count
                FROM db_info LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                print(f"  Version: {row[0]} (schema v{row[1]})")
                print(f"  Built: {row[2]}")
                print(f"  db_info — books: {row[3]:,}, "
                      f"pages: {row[4]:,}, tokens: {row[5]:,}")

        cur.execute("SELECT COUNT(*) FROM page_tokens")
        print(f"  Page tokens: {cur.fetchone()[0]:,}")
        enc = _page_tokens_encoding_counts(corpus_conn)
        print("  Blob encodings: " + ", ".join(
            f"{k}={v:,}" for k, v in sorted(enc.items())))
        if _table_exists(corpus_conn, "token_codec"):
            cur.execute("SELECT key, LENGTH(data), meta FROM token_codec ORDER BY key")
            for key, size, meta in cur.fetchall():
                print(f"  token_codec.{key}: {size:,} bytes {meta or ''}")

        cur.execute("SELECT COUNT(DISTINCT book_id) FROM page_tokens")
        print(f"  Books with tokens: {cur.fetchone()[0]:,}")

        cur.execute("SELECT COUNT(*) FROM token_definitions")
        unique = cur.fetchone()[0]
        print(f"  Unique token definitions: {unique:,}")

        print("  Lookup tables:")
        for table in ["lemmas", "roots", "pos_types", "feature_sets", "clitic_sets"]:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            print(f"    {table}: {cur.fetchone()[0]:,}")

        corpus_conn.close()
    else:
        print(f"\n[corpus.db] NOT FOUND at {CORPUS_DB_PATH}")

    if METADATA_DB_PATH.exists():
        print(f"\n[metadata.db] {METADATA_DB_PATH}")
        print(f"  Size: {METADATA_DB_PATH.stat().st_size / (1024 * 1024):.2f} MB")

        meta_conn = sqlite3.connect(METADATA_DB_PATH)
        cur = meta_conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='db_info'")
        if cur.fetchone():
            cur.execute("""
                SELECT corpus_version, schema_version, built_at,
                       book_count, page_count, token_count
                FROM db_info LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                print(f"  Version: {row[0]} (schema v{row[1]})")
                print(f"  Built: {row[2]}")
                print(f"  db_info — books: {row[3]:,}, "
                      f"pages: {row[4]:,}, tokens: {row[5]:,}")

        cur.execute("SELECT COUNT(*) FROM books")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM books WHERE in_corpus = 1")
        in_corpus = cur.fetchone()[0]
        print(f"  Books: {total:,} ({in_corpus:,} in_corpus=1, "
              f"{total - in_corpus:,} excluded)")

        cur.execute("SELECT COUNT(*) FROM authors")
        print(f"  Authors: {cur.fetchone()[0]:,}")

        cur.execute("SELECT COUNT(*) FROM genres")
        print(f"  Genres: {cur.fetchone()[0]:,}")

        cur.execute("PRAGMA table_info(books)")
        has_parts = any(r[1] == 'parts' for r in cur.fetchall())

        if has_parts:
            cur.execute(
                "SELECT SUM(page_count), SUM(token_count), SUM(parts) "
                "FROM books WHERE in_corpus = 1"
            )
            total_pages, total_tokens, total_parts = cur.fetchone()
            print(f"  Total pages (in_corpus=1):  {total_pages or 0:,}")
            print(f"  Total tokens (in_corpus=1): {total_tokens or 0:,}")
            print(f"  Total parts (in_corpus=1):  {total_parts or 0:,}")
        else:
            cur.execute(
                "SELECT SUM(page_count), SUM(token_count) FROM books WHERE in_corpus = 1"
            )
            total_pages, total_tokens = cur.fetchone()
            print(f"  Total pages (in_corpus=1):  {total_pages or 0:,}")
            print(f"  Total tokens (in_corpus=1): {total_tokens or 0:,}")
            print(f"  Total parts: (column missing — run --update-counts)")

        print("\n  Recent books:")
        if has_parts:
            cur.execute("""
                SELECT id, title, page_count, token_count, parts, in_corpus
                FROM books ORDER BY id DESC LIMIT 5
            """)
            for row in cur.fetchall():
                title = row[1][:40] if row[1] else "Unknown"
                in_corpus_tag = "" if row[5] == 1 else " [in_corpus=0]"
                print(f"    [{row[0]}] {title}: {row[2]:,} pages, "
                      f"{row[3]:,} tokens, {row[4]} parts{in_corpus_tag}")
        else:
            cur.execute("""
                SELECT id, title, page_count, token_count, in_corpus
                FROM books ORDER BY id DESC LIMIT 5
            """)
            for row in cur.fetchall():
                title = row[1][:40] if row[1] else "Unknown"
                in_corpus_tag = "" if row[4] == 1 else " [in_corpus=0]"
                print(f"    [{row[0]}] {title}: {row[2]:,} pages, "
                      f"{row[3]:,} tokens{in_corpus_tag}")

        meta_conn.close()
    else:
        print(f"\n[metadata.db] NOT FOUND at {METADATA_DB_PATH}")

    print("=" * 70)


def check_integrity():
    """Check database integrity and consistency."""
    print("=" * 70)
    print("INTEGRITY CHECK")
    print("=" * 70)

    errors = []

    if not CORPUS_DB_PATH.exists():
        print("corpus.db not found")
        errors.append("corpus.db missing")
    else:
        corpus_conn = sqlite3.connect(CORPUS_DB_PATH)
        corpus_cur = corpus_conn.cursor()

        print("Checking for duplicate page_tokens...")
        corpus_cur.execute("""
            SELECT book_id, part_index, page_id, COUNT(*) as cnt
            FROM page_tokens
            GROUP BY book_id, part_index, page_id
            HAVING cnt > 1
        """)
        dupes = corpus_cur.fetchall()
        if dupes:
            errors.append(f"Found {len(dupes)} duplicate page_token keys")
            for d in dupes[:5]:
                print(f"  Duplicate: book={d[0]}, part={d[1]}, page={d[2]}")
        else:
            print("  [OK] No duplicate page_tokens")

        print("Checking blob encodings...")
        enc = _page_tokens_encoding_counts(corpus_conn)
        unknown = {k: v for k, v in enc.items()
                   if k not in (ENCODING_RAW_U32, ENCODING_RANK_VARINT_ZSTD)}
        if unknown:
            errors.append(f"Unknown page_tokens encodings: {unknown}")
        n_v2 = enc.get(ENCODING_RANK_VARINT_ZSTD, 0)
        if n_v2:
            codec = load_codec(corpus_conn)
            if codec is None:
                errors.append("encoding-2 blobs present but token_codec is missing")
            else:
                corpus_cur.execute(
                    "SELECT book_id, part_index, page_id, token_ids FROM page_tokens "
                    f"WHERE encoding = {ENCODING_RANK_VARINT_ZSTD} ORDER BY RANDOM() LIMIT 500"
                )
                bad = 0
                for b, p, g, blob in corpus_cur.fetchall():
                    try:
                        ids = codec.decode(blob)
                        if len(ids) != blob_token_count(ENCODING_RANK_VARINT_ZSTD, blob):
                            bad += 1
                    except Exception as e:  # noqa: BLE001
                        bad += 1
                        if bad <= 3:
                            print(f"  decode error book={b} part={p} page={g}: {e}")
                if bad:
                    errors.append(f"{bad}/500 sampled encoding-2 blobs failed to decode")
                else:
                    print("  [OK] 500 sampled encoding-2 blobs decode")
        print(f"  Encodings present: {enc}")

        corpus_conn.close()

    if not METADATA_DB_PATH.exists():
        print("metadata.db not found")
        errors.append("metadata.db missing")
    else:
        meta_conn = sqlite3.connect(METADATA_DB_PATH)
        meta_cur = meta_conn.cursor()

        print("Checking author references...")
        meta_cur.execute("""
            SELECT COUNT(*) FROM books b
            WHERE b.author_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM authors a WHERE a.id = b.author_id)
        """)
        invalid = meta_cur.fetchone()[0]
        if invalid:
            errors.append(f"Found {invalid} books with invalid author_id")
        else:
            print("  [OK] All author references valid")

        print("Checking genre references...")
        meta_cur.execute("""
            SELECT COUNT(*) FROM books b
            WHERE b.genre_id IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM genres g WHERE g.id = b.genre_id)
        """)
        invalid = meta_cur.fetchone()[0]
        if invalid:
            errors.append(f"Found {invalid} books with invalid genre_id")
        else:
            print("  [OK] All genre references valid")

        meta_conn.close()

    if CORPUS_DB_PATH.exists() and METADATA_DB_PATH.exists():
        print("Checking cross-database consistency...")
        corpus_conn = sqlite3.connect(CORPUS_DB_PATH)
        meta_conn = sqlite3.connect(METADATA_DB_PATH)

        corpus_cur = corpus_conn.cursor()
        meta_cur = meta_conn.cursor()

        corpus_cur.execute("SELECT DISTINCT book_id FROM page_tokens")
        books_with_tokens = {row[0] for row in corpus_cur.fetchall()}

        meta_cur.execute("SELECT id FROM books WHERE in_corpus = 1")
        books_in_corpus = {row[0] for row in meta_cur.fetchall()}

        missing_tokens = books_in_corpus - books_with_tokens
        if missing_tokens:
            errors.append(f"Found {len(missing_tokens)} in_corpus=1 books without token data")
            print(f"  Books missing tokens: {sorted(list(missing_tokens))[:10]}...")
        else:
            print("  [OK] All in_corpus=1 books have token data")

        meta_cur.execute("SELECT id FROM books WHERE in_corpus = 0")
        books_excluded = {row[0] for row in meta_cur.fetchall()}
        wrongly_included = books_with_tokens & books_excluded
        if wrongly_included:
            errors.append(
                f"Found {len(wrongly_included)} books with tokens in corpus.db "
                f"but in_corpus=0 in metadata.db"
            )
            print(f"  Wrongly-included: {sorted(list(wrongly_included))[:10]}...")
        else:
            print("  [OK] No in_corpus=0 books have token data")

        meta_cur.execute("SELECT id FROM books")
        all_meta_ids = {row[0] for row in meta_cur.fetchall()}
        orphan_tokens = books_with_tokens - all_meta_ids
        if orphan_tokens:
            errors.append(
                f"Found {len(orphan_tokens)} books with tokens but no metadata row"
            )
            print(f"  Orphan tokens: {sorted(list(orphan_tokens))[:10]}...")
        else:
            print("  [OK] All books with tokens have metadata rows")

        corpus_conn.close()
        meta_conn.close()

    print()
    if errors:
        print("ERRORS FOUND:")
        for e in errors:
            print(f"  [ERR] {e}")
    else:
        print("[OK] All checks passed")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(
        description="Build SQLite token databases from book JSONL files (multiprocessing version).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Outputs:
  corpus.db   - Token data (page_tokens, token_definitions, lookup tables)
                ONLY contains in_corpus=1 books
  metadata.db - Book/author/genre metadata (ALL books, regardless of in_corpus)

Token, page, and parts counts in metadata.db.books are derived from
page_tokens blobs in corpus.db. The xlsx token_count/page_count columns
are placeholders only. Both DBs' db_info row is also refreshed by
update_token_counts() / --update-counts so totals stay consistent.

Examples:
  %(prog)s --rebuild --corpus-version 1.0.0
  %(prog)s --add --corpus-version 1.0.1
  %(prog)s --add --corpus-version 1.0.1 --books 123 456 --force
  %(prog)s --load-metadata --corpus-version 1.0.0
  %(prog)s --update-counts
  %(prog)s --status
  %(prog)s --check
        """
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--rebuild", action="store_true",
                       help="Delete corpus.db and rebuild from scratch")
    group.add_argument("--add", action="store_true",
                       help="Add new books (incremental)")
    group.add_argument("--load-metadata", action="store_true",
                       help="Load/reload authors, genres, books into metadata.db. "
                            "If corpus.db exists, also refreshes token counts from blobs.")
    group.add_argument("--update-counts", action="store_true",
                       help="Refresh page_count/token_count/parts in metadata.db "
                            "books AND db_info in both DBs, from corpus.db blobs.")
    group.add_argument("--recompress", action="store_true",
                       help="Re-encode page_tokens blobs as encoding 2 (ranked varint + zstd)")
    group.add_argument("--build-triples", action="store_true",
                       help="Build the triples table and token_definitions.triple_id (schema v4, "
                            "needed by the compound Tantivy index)")
    group.add_argument("--status", action="store_true",
                       help="Show database status")
    group.add_argument("--check", action="store_true",
                       help="Check database integrity")

    parser.add_argument("--data-dir", type=Path, metavar="DIR", dest="data_dir",
                        help="Data directory holding processed/, corpus.db, metadata.db "
                             "(default: ./data)")
    parser.add_argument("--keep-corpus-db", action="store_true", dest="keep_corpus_db",
                        help="With --load-metadata/--update-counts: open corpus.db read-only and "
                             "leave its db_info alone (metadata-only reload of a published build)")
    parser.add_argument("--jsonl-dir", type=Path, metavar="DIR", dest="jsonl_dir",
                        help="Read book JSONL from DIR instead of <data-dir>/processed "
                             "(build_corpus.py points this at data/processed)")
    parser.add_argument("--books", type=int, nargs="+", metavar="ID",
                        help="Specific book IDs to add (with --add)")
    parser.add_argument("--force", action="store_true",
                        help="Replace existing book tokens (with --add)")
    parser.add_argument("--corpus-version", type=str, metavar="VERSION", dest="corpus_version",
                        help="Corpus version string (required with --rebuild, --add, --load-metadata)")
    parser.add_argument("--workers", type=int, metavar="N",
                        default=DEFAULT_NUM_WORKERS,
                        help=f"Number of worker processes (default: {DEFAULT_NUM_WORKERS})")

    rc = parser.add_argument_group("--recompress options")
    rc.add_argument("--zstd-level", type=int, default=DEFAULT_ZSTD_LEVEL, metavar="N",
                    help=f"zstd compression level (default {DEFAULT_ZSTD_LEVEL})")
    rc.add_argument("--dict-size", type=int, default=DEFAULT_DICT_SIZE, metavar="BYTES",
                    help=f"zstd dictionary size (default {DEFAULT_DICT_SIZE})")
    rc.add_argument("--dict-samples", type=int, default=DEFAULT_DICT_SAMPLES, metavar="N",
                    help=f"pages sampled for dictionary training (default {DEFAULT_DICT_SAMPLES})")
    rc.add_argument("--retrain", action="store_true",
                    help="Rebuild the rank map and dictionary even if token_codec exists")
    rc.add_argument("--verify-sample", type=int, default=DEFAULT_VERIFY_SAMPLE, metavar="N",
                    help=f"pages round-trip-verified before the table swap (default {DEFAULT_VERIFY_SAMPLE})")
    rc.add_argument("--fixtures", type=Path, metavar="DIR",
                    help="Also write decoder fixtures (blobs_v2.jsonl, zstd_dict.bin, rank_to_def.bin) to DIR")
    rc.add_argument("--no-vacuum", action="store_true",
                    help="Skip the final VACUUM")

    args = parser.parse_args()

    if args.data_dir:
        set_data_dir(args.data_dir)
    if args.keep_corpus_db:
        global KEEP_CORPUS_DB
        KEEP_CORPUS_DB = True
    if args.jsonl_dir:
        global BOOKS_DIR
        BOOKS_DIR = Path(args.jsonl_dir).resolve()

    if args.books and not args.add:
        parser.error("--books requires --add")

    if args.force and not args.add:
        parser.error("--force requires --add")

    if (args.rebuild or args.add or args.load_metadata) and not args.corpus_version:
        parser.error("--rebuild, --add, and --load-metadata require --corpus-version")

    if args.status:
        show_status()
    elif args.check:
        check_integrity()
    elif args.update_counts:
        update_counts_only()
    elif args.build_triples:
        build_triples(vacuum=not args.no_vacuum)
    elif args.recompress:
        recompress_page_tokens(
            level=args.zstd_level,
            dict_size=args.dict_size,
            dict_samples=args.dict_samples,
            retrain=args.retrain,
            verify_sample=args.verify_sample,
            fixtures_dir=args.fixtures,
            vacuum=not args.no_vacuum,
        )
    elif args.load_metadata:
        meta_conn = connect_db(METADATA_DB_PATH, large_cache=False)
        init_metadata_schema(meta_conn)
        load_metadata(meta_conn, args.corpus_version)
        meta_conn.close()
        print("Metadata loaded into metadata.db.")

        if CORPUS_DB_PATH.exists():
            print("\nRefreshing token counts from corpus.db blobs...")
            corpus_conn = open_corpus_for_counts()
            metadata_conn = connect_db(METADATA_DB_PATH, large_cache=False)
            cur = corpus_conn.cursor()
            cur.execute("SELECT COUNT(*) FROM page_tokens")
            n = cur.fetchone()[0]
            if n > 0:
                update_token_counts(corpus_conn, metadata_conn, zero_missing=True,
                                    corpus_version_override=args.corpus_version)
            else:
                print("  corpus.db has no page_tokens — skipping count refresh")
            corpus_conn.close()
            metadata_conn.close()
        else:
            print("\nNote: corpus.db does not exist yet. token_count, "
                  "page_count, and parts are 0; run --rebuild or "
                  "--update-counts after building corpus.db.")
    elif args.rebuild:
        build_database(rebuild=True, version=args.corpus_version, num_workers=args.workers)
    elif args.add:
        build_database(
            rebuild=False,
            book_ids=args.books,
            force=args.force,
            version=args.corpus_version,
            num_workers=args.workers
        )


if __name__ == "__main__":
    main()