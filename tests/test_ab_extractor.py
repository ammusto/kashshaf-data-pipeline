#!/usr/bin/env python3
"""ab_extractor against the shapes Ahl al-Bayt books actually take."""
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "extract"))

import ab_extractor as ab  # noqa: E402


def test_death_plain_hijri():
    d = ab.parse_death("1314", None)
    assert (d.ah, d.basis) == (1314, "ah")
    assert ab.died_by(d, 1348)
    assert not ab.died_by(ab.parse_death("1390", None), 1348)


def test_death_cutoff_is_inclusive():
    assert ab.died_by(ab.parse_death("1348", None), 1348)
    assert not ab.died_by(ab.parse_death("1349", None), 1348)


def test_death_approximate_forms():
    for era, year in (("نحو سنة 450", 450), ("حي سنة 1100", 1100), ("بعد 705", 705)):
        d = ab.parse_death(era, None)
        assert (d.ah, d.basis) == (year, "ah_approx"), era


def test_death_century():
    d = ab.parse_death("قرن 9", None)
    assert (d.ah, d.century, d.basis) == (None, 9, "century")
    assert ab.died_by(d, 1348)
    assert not ab.died_by(ab.parse_death("قرن 14", None), 1348)
    assert ab.parse_death("أوائل قرن 11", None).century == 11


def test_death_other_calendars():
    assert ab.parse_death("1350 ش", None).basis == "solar_hijri"
    assert ab.parse_death("ش 1350", None).ah == ab.ce_to_ah(1971)
    d = ab.parse_death("1900 م", None)
    assert d.basis == "ce" and d.ah == ab.ce_to_ah(1900)
    bce = ab.parse_death("322 ق م", None)
    assert bce.basis == "bce" and ab.died_by(bce, 1348)


def test_death_contemporary_and_unknown():
    assert not ab.died_by(ab.parse_death("معاصر", None), 1348)
    assert ab.parse_death("حي القيوم", None).basis == "unknown"
    assert ab.parse_death("", None).basis == "unknown"


def test_death_falls_back_to_died_at():
    d = ab.parse_death(None, -2321049600000000)
    assert d.basis == "died_at_ce" and 1310 <= d.ah <= 1316


def test_space_before_punctuation():
    out, _ = ab.normalize_text("قال : هذا ، وذلك . فلم ؟")
    assert out == "قال: هذا، وذلك. فلم؟"


def test_space_inside_brackets():
    out, _ = ab.normalize_text("( 1 ) القصص : 88 . « كُلُّ شَيْءٍ » [ أبغ ]")
    assert out == "(1) القصص: 88. «كُلُّ شَيْءٍ» [أبغ]"


def test_spaced_dots():
    assert ab.normalize_text("النبي والرسول وافتراق الأمة . . .")[0] == "النبي والرسول وافتراق الأمة..."


def test_zero_width_deleted_not_spaced():
    """As a space, the joiner in إل‌ياسين would split the word in two."""
    out, n = ab.normalize_text("سَلامٌ عَلى إِل‌ْياسِينَ و 710 ه‍")
    assert "إِلْياسِينَ" in out and n == 2


def test_hemistich_converted():
    line, n = ab.convert_hemistichs("أنا النبيّ لا كذب * أنا ابن عبد المطلب « 6 »")
    assert line == "أنا النبيّ لا كذب … أنا ابن عبد المطلب « 6 »" and n == 1


def test_single_hemistich_line_left_alone():
    src = "* ذو دَغَياتٍ قُلَّبُ الأخلاقِ * « 1 »"
    assert ab.convert_hemistichs(src) == (src, 0)


def test_match_key_ignores_diacritics_and_numbering():
    assert ab.match_key("7 - بَابُ سُجُودِ التِّلَاوَةِ وَالشُّكْرِ 1:") == ab.match_key("باب سجود التلاوة والشكر")


def lines_of(*texts):
    return [["text", t] for t in texts]


def test_toc_title_wraps_matching_line():
    lines = lines_of("قوله وأما", "7 - بَابُ سُجُودِ التِّلَاوَةِ وَالشُّكْرِ 1:", "حديث")
    stats = Counter()
    ab.place_toc_titles(lines, [("باب سجود التلاوة والشكر", 0)], stats)
    assert lines[1][0] == "title" and "بَابُ" in lines[1][1]
    assert len(lines) == 3 and stats["toc_matched_line"] == 1


def test_toc_title_absent_is_inserted_on_top():
    lines = lines_of("عَنْ عُثْمَانَ إنَّمَا السَّجْدَةُ", "حديث ابن عباس")
    ab.place_toc_titles(lines, [("باب صلاة التطوع", 0), ("فصل", 0)], Counter())
    assert [l[1] for l in lines[:2]] == ["باب صلاة التطوع", "فصل"]
    assert lines[2][0] == "text"


def test_toc_title_in_long_line_goes_before_it():
    long = "كتاب صلاة الجماعة " + "وهذا كلام طويل في الباب " * 5
    lines = lines_of("مقدمة", long)
    ab.place_toc_titles(lines, [("كتاب صلاة الجماعة", 0)], Counter())
    assert lines[1] == ["title", "كتاب صلاة الجماعة", 0] and lines[2][1] == long


def test_short_toc_title_needs_exact_line():
    """Dictionary letters (`د`, `ب`) are contained in every line."""
    lines = lines_of("دهن الأترج", "د")
    ab.place_toc_titles(lines, [("د", 0)], Counter())
    assert lines[0][0] == "text" and lines[1][0] == "title"


def test_toc_title_already_a_heading():
    lines = [["title", "باب سجود السهو", 0]] + lines_of("نص")
    stats = Counter()
    ab.place_toc_titles(lines, [("باب سجود السهو", 0)], stats)
    assert len(lines) == 2 and stats["toc_already_heading"] == 1


def test_titles_nest_by_level():
    parts = [{"part": "1", "pages": [{"_lines": [
        ["title", "كتاب الصلاة", 0], ["title", "باب الأذان", 1], ["text", "نص"],
        ["title", "باب الإقامة", 1], ["title", "كتاب الصوم", 0]]}]}]
    ab.number_titles(parts)
    assert parts[0]["pages"][0]["body"].split("\n") == [
        "<title id=1 parent=0>كتاب الصلاة</title>",
        "<title id=2 parent=1>باب الأذان</title>",
        "نص",
        "<title id=3 parent=1>باب الإقامة</title>",
        "<title id=4 parent=0>كتاب الصوم</title>",
    ]


SCHEMA = """
create table pages (id integer primary key, number integer, label text, attachment_page_label text);
create table page_contents (id integer primary key, page_number integer, properties json, weight integer default 0,
    language_id text, page_id integer, parent_id integer);
create table page_content_references (id integer primary key, title text, page_number integer, page_label text,
    weight integer default 0, page_id integer, page_content_id integer, parent_id integer);
"""


def make_book(nodes, toc=()):
    """nodes: (id, page_id, parent_id, props)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    pages = sorted({n[1] for n in nodes} | {t[2] for t in toc})
    con.executemany("insert into pages values (?, ?, ?, ?)", [(p, p, str(p), str(p)) for p in pages])
    con.executemany("insert into page_contents (id, page_id, parent_id, properties) values (?, ?, ?, ?)",
                    [(i, pg, par, json.dumps(pr, ensure_ascii=False)) for i, pg, par, pr in nodes])
    con.executemany("insert into page_content_references (id, title, page_id, page_content_id, parent_id) "
                    "values (?, ?, ?, ?, ?)", toc)
    con.commit()
    con.close()
    try:
        return ab.Book(Path(path))
    finally:
        os.remove(path)


def T(s):
    return {"_type": "text", "Content": s}


def test_ref_joins_the_sentence_and_loses_its_wrapper():
    """book 1, page 56: text, a Qur'an ref, then the rest of the sentence."""
    book = make_book([
        (1, 1, None, T("واختلفوا في وقوعه : فقال بعضهم : إنه سيقع ، لقوله تعالى :")),
        (2, 1, None, {"_type": "ref", "Kind": "content", "Parameters": [{"ID": "0001028088", "URI": ""}]}),
        (3, 1, 2, T("« كُلُّ شَيْءٍ هالِكٌ إِلَّا وَجْهَهُ »")),
        (4, 1, None, T("« 1 » ونظائره .\nولا يرد عليهم")),
        (5, 1, None, {"_type": "footnote", "Label": None}),
        (6, 1, 5, T("( 1 ) القصص : 88 .")),
    ])
    stats = Counter()
    lines, fn = ab.Renderer(book, stats, keep_footnotes=False).page_lines(1)
    assert [l[1] for l in lines] == [
        "واختلفوا في وقوعه: فقال بعضهم: إنه سيقع، لقوله تعالى: «كُلُّ شَيْءٍ هالِكٌ إِلَّا وَجْهَهُ» «1» ونظائره.",
        "ولا يرد عليهم",
    ]
    assert "0001028088" not in json.dumps(lines) and "القصص" not in json.dumps(lines)
    assert fn == "" and stats["refs_unwrapped"] == 1 and stats["footnotes_dropped"] == 1


def test_footnotes_kept_on_request():
    book = make_book([(1, 1, None, T("نص « 1 »")), (2, 1, None, {"_type": "footnote", "Label": None}),
                      (3, 1, 2, T("( 1 ) القصص : 88 ."))])
    _, fn = ab.Renderer(book, Counter(), keep_footnotes=True).page_lines(1)
    assert fn == "(1) القصص: 88."


def test_heading_poem_and_remark_are_blocks():
    book = make_book([
        (1, 1, None, {"_type": "heading", "Level": 0}),
        (2, 1, 1, T("العالم بين القدم والحدوث")),
        (3, 1, None, T("في قوله :")),
        (4, 1, None, {"_type": "poem"}),
        (5, 1, 4, T("إليك فما بدر المقنع طالعاً * بأسحر من ألحاظ بدر المعمم")),
        (6, 1, None, {"_type": "remark"}),
        (7, 1, 6, T("وذكر قول عبد اللّه بن رواحة")),
    ], toc=[(1, "العالم بين القدم والحدوث", 1, 1, None)])
    lines, _ = ab.Renderer(book, Counter(), False).page_lines(1)
    assert lines == [
        ["title", "العالم بين القدم والحدوث", 0],
        ["text", "في قوله:"],
        ["text", "إليك فما بدر المقنع طالعاً … بأسحر من ألحاظ بدر المعمم"],
        ["text", "وذكر قول عبد اللّه بن رواحة"],
    ]


def test_toc_only_entries_and_heading_depth():
    book = make_book([(1, 1, None, {"_type": "heading", "Level": 0}), (2, 1, 1, T("كتاب الطهارة")),
                      (3, 2, None, T("نص"))],
                     toc=[(1, "كتاب الطهارة", 1, 1, None), (2, "باب المياه", 2, None, 1)])
    assert book.heading_depth == {1: 0}
    assert book.toc_only == {2: [("باب المياه", 1)]}


def dates(raw):
    d = ab.parse_print_year(raw)[0]
    return d["hijri"], d["gregorian"]


def test_print_year_lunar_hijri():
    assert dates("1423 ق") == ("1423", None)


def test_print_year_gregorian():
    assert dates("1982 م") == (None, "1982")


def test_print_year_both_with_marker_moved_by_rtl():
    assert dates("م / 1997 ق 1417") == ("1417", "1997")


def test_print_year_solar_hijri_becomes_gregorian():
    assert dates("1387 ه . ش") == (None, "2008")
    assert dates("1375 ش") == (None, "1996")


def test_print_year_reversed_digits():
    assert dates("5241 ه‌. ق") == ("1425", None)


class FakeCatalogue:
    """Just enough of ab.Catalogue for group_volumes."""

    def __init__(self, books):
        # books: id -> (title, publisher, authors, volume_number, label, editors)
        self.books = {i: {"publisher_id": p, "volume_number": n} for i, (_, p, _, n, _, _) in books.items()}
        self._t = {i: {"title": t, "volume_label": lab} for i, (t, _, _, _, lab, _) in books.items()}
        self._a = {i: a for i, (_, _, a, _, _, _) in books.items()}
        self.book_contribs = {i: [{"contribution_id": 1, "contributor_id": c} for c in a] +
                              [{"contribution_id": 50, "contributor_id": e} for e in eds]
                              for i, (_, _, a, _, _, eds) in books.items()}

    def title(self, bid):
        return self._t[bid]

    def authors(self, bid):
        return self._a[bid]


def test_volumes_with_different_editors_are_one_work():
    """al-Qurtubi: a different muhaqqiq on most volumes."""
    cat = FakeCatalogue({1: ("الجامع", 610, [9], 1, "1", [100]), 2: ("الجامع", 610, [9], 2, "2", [101]),
                         3: ("الجامع", 610, [9], 3, "3", [])})
    assert ab.group_volumes(cat, [1, 2, 3]) == [[1, 2, 3]]


def test_split_volume_ordered_by_label():
    """al-Mughni: vol 6 in two parts with no volume_number."""
    cat = FakeCatalogue({1: ("المغني", 1, [9], 5, "5", []), 2: ("المغني", 1, [9], None, "6 ق 2", []),
                         3: ("المغني", 1, [9], None, "6 ق 1", []), 4: ("المغني", 1, [9], 7, "7", [])})
    assert ab.group_volumes(cat, [1, 2, 3, 4]) == [[1, 3, 2, 4]]


def test_volumes_spread_over_publisher_records_are_one_work():
    """Misbah al-faqih 1-14 is filed under six publisher records."""
    cat = FakeCatalogue({1: ("مصباح", 1140, [9], 1, "1", []), 2: ("مصباح", 1141, [9], 2, "2", []),
                         3: ("مصباح", 1140, [9], 3, "3", []), 4: ("مصباح", 823, [9], 4, "4", [])})
    assert ab.group_volumes(cat, [1, 2, 3, 4]) == [[1, 2, 3, 4]]


def test_second_edition_stays_apart():
    """al-Kafi exists eight times; a repeated volume position is another edition."""
    cat = FakeCatalogue({1: ("الكافي", 1, [9], 1, "1", []), 2: ("الكافي", 1, [9], 2, "2", []),
                         3: ("الكافي", 2, [9], 1, "1", []), 4: ("الكافي", 2, [9], 2, "2", [])})
    assert ab.group_volumes(cat, [1, 2, 3, 4]) == [[1, 2], [3, 4]]


def test_unnumbered_books_stay_apart():
    cat = FakeCatalogue({1: ("رسالة", 1, [9], None, None, []), 2: ("رسالة", 2, [9], None, None, [])})
    assert ab.group_volumes(cat, [1, 2]) == [[1], [2]]


def test_different_authors_never_join():
    cat = FakeCatalogue({1: ("الأمالي", 1, [9], 1, "1", []), 2: ("الأمالي", 1, [8], 2, "2", [])})
    assert ab.group_volumes(cat, [1, 2]) == [[1], [2]]


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
