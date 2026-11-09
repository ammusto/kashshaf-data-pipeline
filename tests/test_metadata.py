#!/usr/bin/env python3
"""Seeding metadata.xlsx from the source tables, and the build's check of it."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_corpus  # noqa: E402
import collect_metadata as cm  # noqa: E402


def _source(corpus, rows):
    """A source table: (original_id, title, author_name, author_key, death_ah) per row."""
    return pd.DataFrame([{"corpus": corpus, "original_id": o, "title": t, "author_name": a,
                          "author_key": k, "death_ah": d, "category": "", "paginated": "1",
                          "tags": "[]", "book_meta": "[]", "author_meta": "[]"}
                         for o, t, a, k, d in rows])


def _seed(sources, master=None):
    tmp = Path(tempfile.mkdtemp()) / "metadata.xlsx"
    if master is not None:
        master.to_excel(tmp, index=False)
    cm.MASTER = tmp
    cm.seed_master(sources)
    return pd.read_excel(tmp, dtype=str, keep_default_na=False)


def test_new_master_numbers_books_and_groups_authors():
    m = _seed({"shamela": _source("shamela", [("10", "كتاب أ", "ابن فلان", "77", "300"),
                                              ("11", "كتاب ب", "ابن فلان", "77", "300"),
                                              ("12", "كتاب ج", "أبو علان", "78", "")])})
    assert list(m["id"]) == ["1", "2", "3"]
    assert m["author_id"][0] == m["author_id"][1] != m["author_id"][2]
    assert list(m["in_corpus"]) == ["1", "1", "1"]
    assert m["century_ah"][0] == "4" and m["century_ah"][2] == ""
    assert (m["genre_id"] == "").all(), "genre is left for the user"


def test_existing_rows_are_kept_and_authors_matched():
    master = pd.DataFrame([{c: "" for c in cm.MASTER_COLUMNS} | {
        "id": "500", "corpus": "openiti", "original_id": "0300Fulan.Kitab.X-ara1", "title": "عنوان مصحح",
        "author_id": "42", "author_name": "فلان المصحح", "genre_id": "3", "genre": "fiqh", "in_corpus": "0"}])
    m = _seed({"openiti": _source("openiti", [
        ("0300Fulan.Kitab.X-ara1", "عنوان", "فلان", "0300Fulan", "300"),
        ("0300Fulan.Akhar.Y-ara1", "كتاب آخر", "فلان", "0300Fulan", "300"),
    ]), "nusus": _source("nusus", [("000001", "رسالة", "فلان المصحح", "فلان المصحح", "")])},
        master)
    old = m[m["id"] == "500"].iloc[0]
    assert (old["title"], old["in_corpus"], old["genre"]) == ("عنوان مصحح", "0", "fiqh"), "never rewritten"
    assert list(m["id"]) == ["500", "501", "502"]
    assert set(m["author_id"]) == {"42"}, "matched by author key, then by name"
    assert set(m["author_name"]) == {"فلان المصحح"}, "the master's name wins"


def _master_file(rows):
    tmp = Path(tempfile.mkdtemp()) / "metadata.xlsx"
    base = {c: "" for c in build_corpus.COLUMNS}
    pd.DataFrame([base | r for r in rows]).to_excel(tmp, index=False)
    return tmp


GOOD = {"id": "1", "corpus": "nusus", "original_id": "000001", "in_corpus": "1", "title": "رسالة",
        "author_id": "1", "author_name": "فلان", "genre_id": "2", "genre": "adab", "paginated": "1",
        "tags": json.dumps([])}


def test_check_accepts_a_complete_master():
    assert build_corpus.check_metadata(_master_file([GOOD])) == []


def test_check_accepts_a_master_with_no_genres():
    assert build_corpus.check_metadata(_master_file([GOOD | {"genre_id": "", "genre": ""}])) == []


def test_check_refuses_half_a_genre_and_clashing_names():
    problems = build_corpus.check_metadata(_master_file([
        GOOD | {"genre": ""},
        GOOD | {"id": "2", "original_id": "000002", "author_name": "علان"},
    ]))
    text = " ".join(problems)
    assert "only one of `genre_id` and `genre`" in text
    assert "`author_id` with more than one `author_name`" in text


def test_check_refuses_repeated_ids_and_files():
    problems = build_corpus.check_metadata(_master_file([GOOD, GOOD]))
    text = " ".join(problems)
    assert "`id` repeats" in text and "share a corpus and original_id" in text


def test_check_ignores_blank_title_outside_the_corpus():
    assert build_corpus.check_metadata(_master_file([
        GOOD, GOOD | {"id": "2", "original_id": "000002", "in_corpus": "0", "title": ""},
    ])) == []


def test_settings_reads_the_config_named_by_the_environment():
    cfg = Path(tempfile.mkdtemp()) / "c.toml"
    cfg.write_text('[paths]\ndata = "D:/elsewhere"\n[sources]\nshamela = "D:/shamela4"\n', encoding="utf-8")
    code = "import settings; print(settings.DATA.as_posix(), settings.METADATA.name, settings.SHAMELA_APP.as_posix(), settings.AHLBAYT_APP)"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "scripts", capture_output=True, text=True,
                         env=os.environ | {"KASHSHAF_CONFIG": str(cfg)}).stdout.split()
    assert out == ["D:/elsewhere", "metadata.xlsx", "D:/shamela4", "None"], out


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
