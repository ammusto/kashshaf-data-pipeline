#!/usr/bin/env python3
"""clean_body_v2 against the cases the September audit actually found."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from body_clean import clean_body_v2  # noqa: E402

ARABIC = re.compile(r"[؀-ۿ]")


def has_no_latin(s: str) -> bool:
    return not re.search(r"[A-Za-z]", s)


def test_had_control_word():
    """`HAD01` is an OpenITI hadith marker, not a word."""
    out = clean_body_v2("حدثنا HAD01 أبو بكر")
    assert "HAD" not in out
    assert "حدثنا" in out and "أبو بكر" in out
    assert "حدثناأبو" not in out


def test_at_markers():
    """`@@`, `@QUR02`, `@HAD` are structural markers."""
    for src in ("قال @@ ثم", "قال @QUR02 ثم", "قال @HAD03 ثم", "قال @ ثم"):
        out = clean_body_v2(src)
        assert "@" not in out, src
        assert out.startswith("قال") and out.endswith("ثم"), (src, out)
        assert "قالثم" not in out


def test_inr_url():
    """`inr://` links were left by the Shamela exporter."""
    out = clean_body_v2('انظر inr://man-1234 في الكتاب')
    assert "inr://" not in out and has_no_latin(out)
    assert "انظر" in out and "في الكتاب" in out


def test_footnote_rule_and_anchor():
    """`¬______` is a rule; `(¬١)` is an anchor and keeps its number."""
    assert "¬" not in clean_body_v2("النص ¬______ الحاشية")
    out = clean_body_v2("النص (¬١) الحاشية")
    assert "(١)" in out and "¬" not in out


def test_equals_rule():
    out = clean_body_v2("النص ==== الحاشية")
    assert "=" not in out
    assert "النص" in out and "الحاشية" in out


def test_editorial_angle_brackets_survive():
    """`&lt; له &gt;` is an editorial supplement, not a tag."""
    out = clean_body_v2("قال &lt; له &gt; الرجل")
    assert "<" not in out and ">" not in out
    assert "له" in out
    assert "‹" in out and "›" in out


def test_doubled_entities_become_guillemets():
    out = clean_body_v2("قال &lt;&lt;النص&gt;&gt; هنا")
    assert "«" in out and "»" in out
    assert "&lt;" not in out and "&gt;" not in out


def test_unclosed_markdown_image():
    """A page break cut an image reference in half, leaving `![](`."""
    out = clean_body_v2("النص ![](")
    assert "![](" not in out
    assert out.strip() == "النص"
    assert "img" not in clean_body_v2("النص ![alt](img/p1.png) بعده")


def test_file_reference():
    out = clean_body_v2("انظر page001.htm وما بعده")
    assert ".htm" not in out and has_no_latin(out)
    assert "انظر" in out and "وما بعده" in out


def test_title_tags_are_untouched():
    """The heading tree depends on these ids; they pass through exactly."""
    src = '<title id=7 parent=3>باب الزهد</title> ثم قال'
    out = clean_body_v2(src)
    assert "<title id=7 parent=3>" in out
    assert "</title>" in out
    assert "باب الزهد" in out


def test_other_tags_are_removed_but_arabic_named_ones_are_not():
    assert "span" not in clean_body_v2('قال <span class="x">النص</span> هنا')
    assert "رمز" in clean_body_v2("قال <رمز> النص")


def test_br_becomes_a_line_break():
    out = clean_body_v2("سطر<br/>سطر آخر")
    assert "\n" in out and "br" not in out


JOINED = "«وقال -أيضًا-ابن جرير:»"


def test_v2_separates_words_a_dash_ran_together():
    out = clean_body_v2(JOINED)
    assert "أيضًا" in out
    assert "ابن" in out
    assert "أيضًاابن" not in out
    assert re.search(r"أيضًا\s+ابن", out), out


def test_markup_and_joining_dashes_together():
    out = clean_body_v2("حدثنا HAD01 أبو -بكر-الصديق ==== هنا")
    assert "HAD" not in out and "=" not in out
    assert re.search(r"بكر\s+الصديق", out)


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
