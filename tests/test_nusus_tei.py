#!/usr/bin/env python3
"""nusus_tei against the 69 books the corpus shipped through 4.4.0."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from nusus_tei import TeiError, tei_parts  # noqa: E402

EXPECTED = Path(__file__).resolve().parent / "nusus_tei_expected.json"
TEI = ROOT / "data/raw/nusus"


def _tei(body: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-8")
    f.write(f'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>{body}</body></text></TEI>')
    f.close()
    return Path(f.name)


def test_pages_lines_and_headings():
    path = _tei('<div><pb n="81"/><p> بسم الله\n   الرحمن </p><p>  </p></div>'
                '<div type="chapter" n="1"><pb n="82"/><head> فصل </head><p>قال</p>'
                '<pb n="83"/><p>ثم</p></div>')
    assert tei_parts(path) == [{"part": "1", "pages": [
        {"page_id": "1", "page_number": "81", "body": "بسم الله الرحمن"},
        {"page_id": "2", "page_number": "82", "body": "<title id=1 parent=0>فصل</title>\nقال"},
        {"page_id": "3", "page_number": "83", "body": "ثم"},
    ]}]


def test_unknown_structure_raises():
    for body in ('<p>قبل</p><pb n="1"/>',
                 '<pb n="1"/><div type="chapter" n="1">نص</div>',
                 '<div n="1"><div n="2"><pb n="1"/></div></div>',
                 '<pb n="1"/><head>عنوان</head>',
                 '<pb n="1"/><lg><l>بيت</l></lg>'):
        try:
            tei_parts(_tei(body))
        except TeiError:
            continue
        raise AssertionError(f"accepted {body}")


def test_matches_shipped_books():
    if not any(TEI.glob("*.xml")):
        print("  (skipped: no TEI in data/raw/nusus/)")
        return
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    bad = []
    for original_id, digest in expected.items():
        parts = tei_parts(TEI / f"{int(original_id)}.xml")
        got = hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if got != digest:
            bad.append(original_id)
    assert not bad, f"{len(bad)} of {len(expected)} differ: {bad[:10]}"
    assert len(expected) == 69, f"expected 69 books, found {len(expected)}"


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
