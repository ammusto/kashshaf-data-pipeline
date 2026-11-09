#!/usr/bin/env python3
"""Read the metadata each source carries, and seed metadata/metadata.xlsx from it.

    python scripts/collect_metadata.py                 # source tables, then the master
    python scripts/collect_metadata.py --sources-only  # source tables only
    python scripts/collect_metadata.py --openiti-tsv path/to/OpenITI_metadata.tsv
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from settings import DATA, METADATA, METADATA_DIR  # noqa: E402

RAW = DATA / "raw"
SOURCES = METADATA_DIR / "sources"
MASTER = METADATA
ZENODO_LATEST = "https://zenodo.org/api/records/17767721/versions/latest"
TEI = "{http://www.tei-c.org/ns/1.0}"

COMMON = ["corpus", "original_id", "title", "author_name", "author_key", "death_ah",
          "category", "paginated", "tags", "book_meta", "author_meta"]
MASTER_COLUMNS = ["id", "corpus", "title", "title_original", "author_id", "author_name",
                  "author_original", "death_ah", "century_ah", "genre_id", "genre",
                  "page_count", "token_count", "paginated", "original_id", "tags",
                  "book_meta", "author_meta", "in_corpus", "parts", "metadata_json",
                  "citation_json"]


def jl(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def century(death) -> str:
    try:
        return str(int(death) // 100 + 1)
    except (TypeError, ValueError):
        return ""


def openiti_uri(filename: str) -> str:
    for ext in (".completed", ".inProgress", ".inprogress", ".mARkdown"):
        if filename.endswith(ext):
            return filename[: -len(ext)]
    return filename


def openiti_tsv(path: Path | None = None) -> Path:
    """The OpenITI release metadata: `path`, the newest TSV already in
    metadata/sources/, or the latest release downloaded from Zenodo."""
    if path:
        return path
    have = sorted(SOURCES.glob("OpenITI_metadata_*.tsv"))
    if have:
        return have[-1]
    with urllib.request.urlopen(ZENODO_LATEST, timeout=60) as r:
        record = json.load(r)
    f = next(f for f in record["files"] if f["key"].startswith("OpenITI_metadata") and f["key"].endswith(".tsv"))
    out = SOURCES / f["key"]
    print(f"  downloading {f['key']} (OpenITI {record['metadata'].get('version')}) from Zenodo")
    SOURCES.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(f["links"]["self"], out)
    return out


def first(s: str) -> str:
    """OpenITI joins alternatives with ` :: `; the first is the fullest."""
    return s.split(" :: ")[0].strip()


def collect_openiti(tsv: Path | None) -> pd.DataFrame:
    files = [f for f in (RAW / "openiti").iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        return pd.DataFrame()
    t = pd.read_csv(openiti_tsv(tsv), sep="\t", dtype=str, keep_default_na=False).set_index("version_uri")
    rows = []
    for f in sorted(files):
        uri = openiti_uri(f.name)
        r = t.loc[uri] if uri in t.index else None
        names = (r["author_ar"].split(" :: ") if r is not None else [])
        text = f.read_text(encoding="utf-8", errors="replace")
        rows.append({
            "corpus": "openiti", "original_id": uri,
            "title": (first(r["title_ar"]) if r is not None else "") or uri.split(".")[1],
            # the last form is the short name the author is known by
            "author_name": (names[-1].strip() if names else "") or uri.split(".")[0],
            "author_key": uri.split(".")[0],
            "death_ah": str(int(r["date"])) if r is not None and r["date"].isdigit() else "",
            "category": "",
            "paginated": "1" if "PageV" in text else "0",
            "tags": jl([x.strip() for x in r["tags"].split(" :: ") if x.strip()] if r is not None else []),
            "book_meta": jl([f"{k}: {r[k]}" for k in ("title_lat", "ed_info", "status") if r is not None and r[k]]),
            "author_meta": jl([f"{k}: {r[k]}" for k in ("author_lat", "author_lat_shuhra", "author_lat_full_name")
                               if r is not None and r[k]]),
            "in_release_metadata": "1" if r is not None else "0",
            "author_ar": r["author_ar"] if r is not None else "",
            "title_ar": r["title_ar"] if r is not None else "",
            "ed_info": r["ed_info"] if r is not None else "",
            "tok_length": r["tok_length"] if r is not None else "",
            "file": f.name,
        })
    return pd.DataFrame(rows)


def collect_shamela() -> pd.DataFrame:
    rows = []
    for f in sorted((RAW / "shamela").glob("*.json"), key=lambda p: (len(p.stem), p.stem)):
        d = json.loads(f.read_text(encoding="utf-8"))
        authors = d.get("authors") or []
        main = next((a for a in authors if a.get("is_main_author")), authors[0] if authors else {})
        cat = (d.get("category") or {}).get("name", "")
        rows.append({
            "corpus": "shamela", "original_id": str(d.get("book_id", f.stem)),
            "title": d.get("title", ""),
            "author_name": main.get("name", ""),
            "author_key": str(main.get("id", "")),
            "death_ah": str(main.get("death_number") or ""),
            "category": cat,
            # Shamela marks books without printed page numbers `printed = 2`
            "paginated": "0" if str(d.get("printed")) == "2" else "1",
            "tags": jl([cat] if cat else []),
            "book_meta": jl(d.get("book_meta") or []),
            "author_meta": jl(d.get("author_meta") or []),
            "co_authors": jl([a.get("name") for a in authors if a is not main]),
            "death_text": main.get("death_text", ""),
            "book_type": d.get("book_type", ""), "printed": d.get("printed", ""),
            "pdf_links": jl(d.get("pdf_links") or {}),
            "file": f.name,
        })
    return pd.DataFrame(rows)


def author_meta_id(author_meta: str) -> str:
    """`id: N` from an author_meta list in the Shamela key style, or ''."""
    try:
        items = json.loads(author_meta) if author_meta else []
    except json.JSONDecodeError:
        return ""
    return next((x[4:].strip() for x in items if isinstance(x, str) and x.startswith("id: ")), "")


def collect_ahlbayt() -> pd.DataFrame:
    book = SOURCES / "ahlbayt.xlsx"
    if not any((RAW / "ahlbayt").glob("*.json")):
        return pd.DataFrame()
    if not book.exists():
        sys.exit(f"{book} not found: extract/ab_extractor.py writes it with the raw JSON")
    w = pd.read_excel(book, sheet_name="works", dtype=str, keep_default_na=False)
    have = {p.stem for p in (RAW / "ahlbayt").glob("*.json")}
    w = w[w["original_id"].isin(have)].copy()
    w["corpus"] = "ahlbayt"
    w["author_key"] = w["author_meta"].map(author_meta_id)
    w["category"] = w.get("ab_category_names", "")
    return w


def collect_nusus() -> pd.DataFrame:
    files = sorted((RAW / "nusus").glob("*.xml"), key=lambda p: (len(p.stem), p.stem))
    if not files:
        return pd.DataFrame()
    contrib = {}
    sheet = RAW / "nusus/text_contrib.csv"
    if sheet.exists():
        with open(sheet, encoding="utf-8-sig") as fh:
            contrib = {r["text_id"]: r for r in csv.DictReader(fh)}
    rows = []
    for f in files:
        h = ET.parse(f).getroot().find(f".//{TEI}teiHeader")
        title = (h.findtext(f".//{TEI}titleStmt/{TEI}title") or "").strip()
        author = (h.findtext(f".//{TEI}titleStmt/{TEI}author") or "").strip()
        bibl = (h.findtext(f".//{TEI}sourceDesc/{TEI}bibl") or "").strip()
        c = contrib.get(f.stem, {})
        rows.append({
            "corpus": "nusus", "original_id": f.stem.zfill(6),
            "title": title, "author_name": author, "author_key": author,
            "death_ah": "", "category": "", "paginated": "1",
            "tags": jl([]),
            "book_meta": jl([x for x in (f"Source: {c['source']}" if c.get("source") else "",
                                         f"Contributor: {c['contrib']}" if c.get("contrib") else "",
                                         f"Edition: {bibl}" if bibl else "") if x]),
            "author_meta": jl([]),
            "file": f.name,
        })
    return pd.DataFrame(rows)


def master_author_key(r) -> str:
    """The same author key the source tables carry, read back from a master row."""
    if r["corpus"] == "openiti":
        return r["original_id"].split(".")[0]
    if r["corpus"] in ("shamela", "ahlbayt"):
        return author_meta_id(r.get("author_meta", ""))
    return r.get("author_original", "") or r["author_name"]


def seed_master(sources: dict[str, pd.DataFrame]) -> None:
    if MASTER.exists():
        master = pd.read_excel(MASTER, dtype=str, keep_default_na=False)
    else:
        master = pd.DataFrame(columns=MASTER_COLUMNS)
    for c in MASTER_COLUMNS:
        if c not in master.columns:
            master[c] = ""

    have = set(zip(master["corpus"], master["original_id"]))
    by_key, by_name, names = {}, {}, {}
    for _, r in master.iterrows():
        if r["author_id"]:
            by_key.setdefault((r["corpus"], master_author_key(r)), r["author_id"])
            by_name.setdefault(r["author_name"], r["author_id"])
            names.setdefault(r["author_id"], r["author_name"])
    ids = pd.to_numeric(master["id"], errors="coerce")
    next_id = int(ids.max()) + 1 if ids.notna().any() else 1
    aids = pd.to_numeric(master["author_id"], errors="coerce")
    next_author = int(aids.max()) + 1 if aids.notna().any() else 1

    new, new_authors = [], 0
    for corpus, df in sources.items():
        for _, s in df.iterrows():
            if (corpus, s["original_id"]) in have:
                continue
            key = (corpus, s["author_key"])
            aid = by_key.get(key) or by_name.get(s["author_name"])
            if not aid:
                aid = str(next_author)
                next_author += 1
                new_authors += 1
                names[aid] = s["author_name"]
            by_key.setdefault(key, aid)
            row = {c: "" for c in master.columns}
            row.update({
                "id": str(next_id), "corpus": corpus, "original_id": s["original_id"],
                "title": s["title"], "title_original": s["title"],
                "author_id": aid, "author_name": names[aid], "author_original": s["author_name"],
                "death_ah": s["death_ah"], "century_ah": century(s["death_ah"]),
                "paginated": s["paginated"], "tags": s["tags"], "book_meta": s["book_meta"],
                "author_meta": s["author_meta"], "in_corpus": "1",
            })
            for c in ("metadata_json", "citation_json"):
                if c in s and s[c]:
                    row[c] = s[c]
            new.append(row)
            next_id += 1

    if new or not MASTER.exists():
        master = pd.concat([master, pd.DataFrame(new)], ignore_index=True)
        master.to_excel(MASTER, index=False)
    print(f"\n{MASTER}: {len(master):,} rows, {len(new):,} added, "
          f"{new_authors:,} new authors")
    missing_death = master[(master["in_corpus"] == "1") & (master["death_ah"] == "")]
    if len(missing_death):
        print(f"  {len(missing_death):,} in-corpus rows have no death_ah (allowed, but dates will be blank)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources-only", action="store_true", help="write the source tables, leave the master alone")
    ap.add_argument("--openiti-tsv", type=Path, help="OpenITI release metadata TSV (default: newest in metadata/sources/, else Zenodo)")
    a = ap.parse_args()

    SOURCES.mkdir(parents=True, exist_ok=True)
    tables = {"openiti": lambda: collect_openiti(a.openiti_tsv), "shamela": collect_shamela,
              "ahlbayt": collect_ahlbayt, "nusus": collect_nusus}
    sources = {}
    for corpus, collect in tables.items():
        df = collect()
        if df.empty:
            print(f"  {corpus:8} no raw files")
            continue
        sources[corpus] = df
        if corpus != "ahlbayt":
            cols = COMMON + [c for c in df.columns if c not in COMMON]
            df[cols].to_excel(SOURCES / f"{corpus}.xlsx", index=False)
        print(f"  {corpus:8} {len(df):,} books -> metadata/sources/{corpus}.xlsx")
    if not a.sources_only:
        seed_master(sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
