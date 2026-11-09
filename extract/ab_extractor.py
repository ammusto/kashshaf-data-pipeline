#!/usr/bin/env python3
"""Extract Ahl al-Bayt library books into the raw JSON shape Kashshaf reads.

    python extract/ab_extractor.py                    # Arabic, author died <= 1348 AH
    python extract/ab_extractor.py --dry-run          # selection and metadata only
    python extract/ab_extractor.py --ids 1 2094       # just these library book ids
    python extract/ab_extractor.py --include-footnotes --all-languages
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import zstandard

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from settings import AHLBAYT_APP, DATA, METADATA_DIR  # noqa: E402

DEFAULT_APP = AHLBAYT_APP
DEFAULT_OUT = DATA / "raw" / "ahlbayt"
DEFAULT_METADATA = METADATA_DIR / "sources" / "ahlbayt.xlsx"
DEFAULT_DEDUP = METADATA_DIR / "ab-dedup.xlsx"
DEFAULT_AUTHORS = METADATA_DIR / "ab-authors.xlsx"
DEFAULT_GENRES = METADATA_DIR / "ab-genres.xlsx"
AUTHOR_ROLE = 1  # contributions.id of مؤلف

# `era` is free text (`نحو سنة 450`, `قرن 9`, `1350 ش`, `450 ق م`); `died_at` is the fallback.
EAST_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)
APPROX_WORDS = ("نحو", "حي", "بعد", "قبل", "-")
US_PER_YEAR = 365.2425 * 86400 * 1e6


@dataclass
class Death:
    ah: int | None
    century: int | None
    basis: str


def ce_to_ah(year: int) -> int:
    return round((year - 622) * 33 / 32)


def us_to_year(us: int) -> int:
    return int(1970 + us / US_PER_YEAR)


def parse_death(era: str | None, died_at: int | None) -> Death:
    """Read one author's death from `era`, falling back to `died_at`."""
    e = (era or "").translate(EAST_DIGITS).strip()
    if not e:
        if died_at is not None:
            return Death(ce_to_ah(us_to_year(died_at)), None, "died_at_ce")
        return Death(None, None, "unknown")
    if "معاصر" in e:
        return Death(None, None, "contemporary")
    m = re.search(r"قرن\s*(\d+)", e)
    if m:
        n = int(m.group(1))
        if re.search(r"\sم$", e):
            end_ah = ce_to_ah(n * 100)
            return Death(None, (end_ah - 1) // 100 + 1, "century_ce")
        return Death(None, n, "century")
    nums = re.findall(r"\d+", e)
    if not nums:
        return Death(None, None, "unknown")
    n = int(nums[-1])
    if re.search(r"ق\s*م", e):
        return Death(None, None, "bce")
    if re.search(r"(^|\s)ش(\s|$)", e):
        return Death(ce_to_ah(n + 621), None, "solar_hijri")
    if re.search(r"(^|\s)م(\s|$)", e):
        return Death(ce_to_ah(n), None, "ce")
    approx = any(w in e for w in APPROX_WORDS)
    return Death(n, None, "ah_approx" if approx else "ah")


def died_by(d: Death, max_ah: int) -> bool:
    """True when the author certainly died in or before `max_ah`."""
    if d.basis == "bce":
        return True
    if d.ah is not None:
        return d.ah <= max_ah
    if d.century is not None:
        return d.century * 100 <= max_ah
    return False


def century_of(d: Death) -> int | None:
    if d.ah is not None and d.ah > 0:
        return (d.ah - 1) // 100 + 1
    return d.century


ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
SPACED_DOTS = re.compile(r"\.(?:[^\S\n]*\.){2,}")
SPACE_BEFORE_PUNCT = re.compile(r"[^\S\n]+(?=[،؛:؟!.,])")
SPACE_AFTER_OPEN = re.compile(r"([(\[«{﴿])[^\S\n]+")
SPACE_BEFORE_CLOSE = re.compile(r"[^\S\n]+([)\]»}﴾])")
MULTI_SPACE = re.compile(r"[^\S\n]{2,}")
HEMISTICH = re.compile(r"(?<=[^\s*])[^\S\n]+\*[^\S\n]+(?=[^\s*])")


def normalize_text(s: str) -> tuple[str, int]:
    """Library typesetting -> corpus typesetting. Returns (text, joiners deleted)."""
    s, zw = ZERO_WIDTH.subn("", s)
    s = SPACED_DOTS.sub("...", s)
    s = SPACE_BEFORE_PUNCT.sub("", s)
    s = SPACE_AFTER_OPEN.sub(r"\1", s)
    s = SPACE_BEFORE_CLOSE.sub(r"\1", s)
    s = MULTI_SPACE.sub(" ", s)
    return s.strip(), zw


def convert_hemistichs(line: str) -> tuple[str, int]:
    """` * ` between two hemistichs -> ` … `. A line opening with `*` wraps a
    single hemistich (`* ذو دغيات قلب الأخلاق * « 1 »`) and is left alone."""
    if line.lstrip().startswith("*"):
        return line, 0
    return HEMISTICH.subn(" … ", line)


DIACRITICS = re.compile(r"[\u064B-\u065F\u0670\u0640]")
FOLD = str.maketrans("أإآٱىة", "اااايه")
NON_LETTER = re.compile(r"[^\u0621-\u064A]+")
SHORT_TITLE = 6
LINE_SLACK = 15


def match_key(s: str) -> str:
    s = DIACRITICS.sub("", s).translate(FOLD)
    return NON_LETTER.sub(" ", s).strip()


@dataclass
class Node:
    id: int
    page_id: int
    parent_id: int | None
    weight: int
    props: dict

    @property
    def type(self) -> str:
        return self.props.get("_type", "")


class Book:
    """One decompressed Ahl al-Bayt book database."""

    def __init__(self, path: Path):
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            self.pages = con.execute(
                "select id, number, label from pages order by number, id").fetchall()
            self.nodes: dict[int, Node] = {}
            for i, pg, par, w, pr in con.execute(
                    "select id, page_id, parent_id, weight, properties from page_contents"):
                self.nodes[i] = Node(i, pg, par, w or 0, json.loads(pr) if pr else {})
            self.toc = con.execute(
                "select id, title, page_id, page_content_id, parent_id, weight "
                "from page_content_references order by weight, id").fetchall()
        finally:
            con.close()
        self.kids: dict[int | None, list[Node]] = defaultdict(list)
        for n in self.nodes.values():
            self.kids[n.parent_id].append(n)
        for lst in self.kids.values():
            lst.sort(key=lambda n: (n.weight, n.id))
        self.roots_by_page: dict[int, list[Node]] = defaultdict(list)
        for n in self.kids[None]:
            self.roots_by_page[n.page_id].append(n)
        parent = {t[0]: t[4] for t in self.toc}
        def depth(tid):
            d, seen = 0, set()
            while parent.get(tid) is not None and tid not in seen:
                seen.add(tid)
                tid = parent[tid]
                d += 1
            return d
        self.heading_depth: dict[int, int] = {}
        self.toc_only: dict[int, list[tuple[str, int]]] = defaultdict(list)
        for tid, title, page_id, pcid, _par, _w in self.toc:
            node = self.nodes.get(pcid) if pcid is not None else None
            if node is not None and node.type == "heading":
                self.heading_depth[pcid] = depth(tid)
            elif (title or "").strip():
                self.toc_only[page_id].append((title.strip(), depth(tid)))


class Renderer:
    """Flattens a book's node tree into lines: ['text', s] or ['title', s, level]."""

    def __init__(self, book: Book, stats: Counter, keep_footnotes: bool):
        self.book = book
        self.stats = stats
        self.keep_footnotes = keep_footnotes

    def flatten(self, node: Node) -> str:
        """All text under a node, in order, footnotes excluded."""
        if node.type == "text":
            return node.props.get("Content") or ""
        if node.type == "footnote":
            return ""
        return " ".join(self.flatten(k) for k in self.book.kids[node.id])

    def assemble(self, nodes: list[Node], footnotes: list[str] | None) -> list[list]:
        lines: list[list] = []
        prev = None
        for n in nodes:
            t = n.type
            if t == "footnote":
                self.stats["footnotes_dropped"] += 1
                if footnotes is not None:
                    sub = self.assemble(self.book.kids[n.id], None)
                    footnotes.append("\n".join(x[1] for x in sub))
                continue
            if t == "heading":
                text = " ".join(self.flatten(n).split())
                if text:
                    level = max(int(n.props.get("Level") or 0),
                                self.book.heading_depth.get(n.id, 0))
                    lines.append(["title", text, level])
                    self.stats["headings"] += 1
                prev = t
                continue
            if t == "poem":
                for line in self.assemble(self.book.kids[n.id], footnotes):
                    if line[0] == "text":
                        line[1], k = convert_hemistichs(line[1])
                        self.stats["hemistichs"] += k
                    lines.append(line)
                prev = t
                continue
            if t == "remark":
                lines.extend(self.assemble(self.book.kids[n.id], footnotes))
                prev = t
                continue
            if t == "text":
                text = n.props.get("Content") or ""
            elif t == "ref":
                self.stats["refs_unwrapped"] += 1
                text = self.flatten(n)
            else:
                self.stats[f"unknown_node_{t}"] += 1
                text = self.flatten(n)
            parts = text.split("\n")
            inline = (t == "ref" or prev == "ref") and lines and lines[-1][0] == "text"
            if inline:
                lines[-1][1] = f"{lines[-1][1]} {parts[0]}"
                parts = parts[1:]
            lines.extend(["text", p] for p in parts)
            prev = t
        return lines

    def page_lines(self, page_id: int) -> tuple[list[list], str]:
        footnotes: list[str] | None = [] if self.keep_footnotes else None
        lines = self.assemble(self.book.roots_by_page.get(page_id, []), footnotes)
        out = []
        for line in lines:
            line[1], zw = normalize_text(line[1])
            self.stats["zero_width_deleted"] += zw
            if line[1]:
                out.append(line)
        fn = ""
        if footnotes:
            fn = "\n".join(t for t in (normalize_text(f)[0] for f in footnotes) if t)
        return out, fn


def place_toc_titles(lines: list[list], titles: list[tuple[str, int]], stats: Counter) -> None:
    """Put table-of-contents titles that name no heading onto their page."""
    top = 0
    for title, level in titles:
        key = match_key(title)
        if not key:
            continue
        if any(l[0] == "title" and match_key(l[1]) == key for l in lines):
            stats["toc_already_heading"] += 1
            continue
        hit, hit_key = None, ""
        for i, line in enumerate(lines):
            if line[0] != "text":
                continue
            lk = match_key(line[1])
            if (lk == key) if len(key) < SHORT_TITLE else (key in lk):
                hit, hit_key = i, lk
                break
        if hit is not None and len(hit_key) <= len(key) + LINE_SLACK:
            lines[hit] = ["title", lines[hit][1], level]
            stats["toc_matched_line"] += 1
        elif hit is not None:
            lines.insert(hit, ["title", title, level])
            stats["toc_inserted_before_line"] += 1
            if hit < top:
                top += 1
        else:
            lines.insert(top, ["title", title, level])
            top += 1
            stats["toc_inserted_top"] += 1


def number_titles(parts: list[dict]) -> None:
    """Turn title lines into `<title id=N parent=M>` and pages into body strings."""
    next_id = 1
    stack: list[tuple[int, int]] = []
    for part in parts:
        for page in part["pages"]:
            out = []
            for line in page.pop("_lines"):
                if line[0] != "title":
                    out.append(line[1])
                    continue
                level = line[2]
                while stack and stack[-1][0] >= level:
                    stack.pop()
                parent = stack[-1][1] if stack else 0
                text = " ".join(line[1].split()).replace("<", "‹").replace(">", "›")
                out.append(f"<title id={next_id} parent={parent}>{text}</title>")
                stack.append((level, next_id))
                next_id += 1
            page["body"] = "\n".join(out)


def open_book(zst: Path, tmp_dir: str) -> Book:
    fd, tmp = tempfile.mkstemp(suffix=".sqlite", dir=tmp_dir)
    try:
        with os.fdopen(fd, "wb") as out, open(zst, "rb") as src:
            zstandard.ZstdDecompressor().copy_stream(src, out)
        return Book(Path(tmp))
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def extract_work(spec: dict) -> dict:
    """Build one work's JSON from its volumes. Runs in a worker process."""
    stats: Counter = Counter()
    parts, errors, missing = [], [], []
    page_id = 1
    for vol in spec["volumes"]:
        zst = Path(spec["book_dir"]) / f"{vol['book_id']}.zstd"
        if not zst.exists():
            missing.append(vol["book_id"])
            continue
        try:
            book = open_book(zst, spec["tmp_dir"])
        except Exception as e:
            errors.append(f"{vol['book_id']}: {e}")
            continue
        r = Renderer(book, stats, spec["include_footnotes"])
        pages, pending = [], []
        for pid, number, label in book.pages:
            lines, fn = r.page_lines(pid)
            pending.extend(book.toc_only.get(pid, []))
            if not lines:
                stats["pages_empty"] += 1
                continue
            if pending:
                place_toc_titles(lines, pending, stats)
                pending = []
            page_number = (label or "").strip() or str(number)
            page = {"page_id": str(page_id), "page_number": page_number, "_lines": lines}
            if spec["include_footnotes"]:
                page["footnote"] = fn
            pages.append(page)
            page_id += 1
        if pending and pages:
            for title, level in pending:
                pages[-1]["_lines"].append(["title", title, level])
                stats["toc_appended_end"] += 1
        stats["pages"] += len(pages)
        if pages:
            parts.append({"part": vol["part"], "pages": pages})
    number_titles(parts)
    result = {"original_id": spec["original_id"], "stats": dict(stats),
              "errors": errors, "missing": missing,
              # Pages carry the printed numbers, so every work counts as paginated.
              "paginated": bool(parts), "written": False}
    if parts:
        doc = {"book_id": spec["original_id"], "corpus": "ahlbayt",
               "title": spec["title"], "ab_book_ids": [v["book_id"] for v in spec["volumes"]],
               "paginated": result["paginated"], "ab_stats": dict(stats), "parts": parts}
        out = Path(spec["out_path"])
        tmp = out.with_suffix(".json.part")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out)
        result["written"] = True
    return result


def ts(us):
    """Catalogue timestamps are microseconds since 1970; dates before year 1
    do occur (born_at of ancient authors), so fall back to the year alone."""
    if us is None:
        return None
    try:
        return (datetime(1970, 1, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=us)).isoformat()
    except OverflowError:
        return str(us_to_year(us))


def rows(con, sql, *args):
    cur = con.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur]


class Catalogue:
    """Everything `app.db` says about books, read once."""

    def __init__(self, app_db: Path):
        con = sqlite3.connect(f"file:{app_db.as_posix()}?mode=ro", uri=True)
        try:
            self.books = {r["id"]: r for r in rows(con, "select * from books")}
            self.titles = defaultdict(dict)
            for r in rows(con, "select * from book_translations"):
                self.titles[r["book_id"]][r["language_id"]] = r
            self.languages = defaultdict(list)
            for r in rows(con, "select * from book_languages order by weight"):
                self.languages[r["book_id"]].append(r["language_id"])
            self.categories = defaultdict(list)
            for r in rows(con, "select * from book_categories order by weight"):
                self.categories[r["book_id"]].append(r["category_id"])
            self.book_contribs = defaultdict(list)
            for r in rows(con, "select * from book_contributors where deleted_at is null "
                               "order by weight, id"):
                self.book_contribs[r["book_id"]].append(r)
            self.contributors = {r["id"]: r for r in rows(con, "select * from contributors")}
            self.contributor_tr = defaultdict(dict)
            for r in rows(con, "select * from contributor_translations"):
                self.contributor_tr[r["contributor_id"]][r["language_id"]] = r
            self.roles = {r["id"]: r for r in rows(con, "select * from contributions")}
            for r in rows(con, "select * from contribution_translations"):
                self.roles[r["contribution_id"]].setdefault("names", {})[r["language_id"]] = r["name"]
            self.publishers = {r["id"]: r for r in rows(con, "select * from publishers")}
            for r in rows(con, "select * from publisher_translations"):
                self.publishers[r["publisher_id"]].setdefault("names", {})[r["language_id"]] = r["name"]
                self.publishers[r["publisher_id"]].setdefault("bios", {})[r["language_id"]] = r["bio"]
            self.category_rows = {r["id"]: r for r in rows(con, "select * from categories")}
            for r in rows(con, "select * from category_translations"):
                self.category_rows[r["category_id"]].setdefault("names", {})[r["language_id"]] = r["name"]
            self.language_rows = rows(con, "select * from languages")
            self.attachments = defaultdict(list)
            for r in rows(con, "select a.book_id, a.context, a.weight, f.* from book_attachments a "
                               "join files f on f.id = a.file_id order by a.weight"):
                self.attachments[r.pop("book_id")].append(r)
        finally:
            con.close()

    def title(self, bid: int, lang: str = "ar") -> dict:
        tr = self.titles.get(bid, {})
        return tr.get(lang) or next(iter(tr.values()), {})

    def person(self, cid: int) -> dict:
        tr = self.contributor_tr.get(cid, {})
        return tr.get("ar") or next(iter(tr.values()), {})

    def role_name(self, rid: int, lang: str = "ar") -> str:
        r = self.roles.get(rid, {})
        return r.get("names", {}).get(lang) or r.get("slug", "")

    def authors(self, bid: int) -> list[int]:
        return [c["contributor_id"] for c in self.book_contribs[bid]
                if c["contribution_id"] == AUTHOR_ROLE]

    def death(self, cid: int) -> Death:
        return parse_death(self.person(cid).get("era"), self.contributors.get(cid, {}).get("died_at"))


def select_books(cat: Catalogue, args, book_dir: Path) -> tuple[list[int], list[dict]]:
    """Return (selected ids, excluded rows). Only books in the requested
    languages are reported as excluded; the rest are not candidates at all."""
    langs = None if args.all_languages else set(args.languages)
    wanted = set(args.ids) if args.ids else None
    selected, excluded = [], []
    for bid, b in sorted(cat.books.items()):
        if wanted is not None and bid not in wanted:
            continue
        if langs is not None and not langs & set(cat.languages.get(bid, [])):
            continue
        reason, detail = None, None
        if b["deleted_at"] is not None:
            reason = "deleted"
        elif b["status"] != "published" and not args.include_unpublished:
            reason = f"status {b['status']}"
        elif not args.no_date_filter:
            authors = cat.authors(bid)
            if not authors:
                reason = "no author"
            else:
                for cid in authors:
                    d = cat.death(cid)
                    if not died_by(d, args.max_death_ah):
                        if d.ah is not None or d.century is not None:
                            reason = "author after cutoff"
                        else:
                            reason = f"author {d.basis}"
                        detail = (f"contributor {cid}, era {cat.person(cid).get('era')!r}, "
                                  f"death_ah {d.ah}, century {d.century}, basis {d.basis}")
                        break
        if reason is None and not (book_dir / f"{bid}.zstd").exists():
            reason = "not downloaded"
        if reason:
            excluded.append({"ab_book_id": bid, "title": cat.title(bid).get("title"),
                             "reason": reason, "detail": detail})
        else:
            selected.append(bid)
    return selected, excluded


LABEL_NUMBER = re.compile(r"\d+")


def volume_key(cat: Catalogue, bid: int) -> tuple[int, int] | None:
    """Where a volume sits in its work. A volume split in two has no
    `volume_number`, only a label: `6 ق 1` is (6, 1), after 5 and before 7."""
    n = cat.books[bid]["volume_number"]
    if n is not None:
        return (int(n), 0)
    label = (cat.title(bid).get("volume_label") or "").translate(EAST_DIGITS)
    nums = [int(x) for x in LABEL_NUMBER.findall(label)]
    if not nums:
        return None
    return (nums[0], nums[1] if len(nums) > 1 else 0)


def volume_part(cat: Catalogue, bid: int) -> str:
    """The `part` a volume becomes: its number, else its label, else 1."""
    n = cat.books[bid]["volume_number"]
    if n is not None:
        return str(n)
    return (cat.title(bid).get("volume_label") or "").strip() or "1"


def group_volumes(cat: Catalogue, ids: list[int]) -> list[list[int]]:
    """Combine volumes of one work: same title and authors."""
    def contributors(bid):
        return tuple(sorted((c["contribution_id"], c["contributor_id"])
                            for c in cat.book_contribs[bid]))

    def pieces(members, split_further=True) -> list[list[int]]:
        """One publisher's volumes, as runs with no repeated position."""
        keys = [volume_key(cat, b) for b in members]
        clash = len({k for k in keys if k}) < len([k for k in keys if k])
        if clash and split_further:
            sub = defaultdict(list)
            for b in members:
                sub[contributors(b)].append(b)
            if len(sub) > 1:
                return [p for m in sub.values() for p in pieces(m, False)]
        out, seen = [], {}
        for b in sorted(members):
            k = volume_key(cat, b)
            if k is None or k in seen:
                out.append([b])
            else:
                seen[k] = b
        if seen:
            out.append([seen[k] for k in sorted(seen)])
        return out

    groups = defaultdict(list)
    for bid in ids:
        groups[(cat.title(bid).get("title"), tuple(sorted(cat.authors(bid))))].append(bid)
    works = []
    for members in groups.values():
        if len(members) == 1:
            works.append(members)
            continue
        by_pub = defaultdict(list)
        for b in members:
            by_pub[cat.books[b]["publisher_id"]].append(b)
        units = [p for m in by_pub.values() for p in pieces(m)]
        joined: list[tuple[set, list[int]]] = []
        for unit in sorted(units, key=min):
            keys = {volume_key(cat, b) for b in unit}
            if None in keys:
                works.append(unit)
                continue
            for taken, books in joined:
                if not taken & keys:
                    taken |= keys
                    books.extend(unit)
                    break
            else:
                joined.append((set(keys), list(unit)))
        works.extend(sorted(books, key=lambda b: volume_key(cat, b)) for _, books in joined)
    works.sort(key=lambda w: w[0])
    return works


def jl(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def load_dedup(path: Path) -> dict[int, dict]:
    """original_id -> the decision `ab-dedup.xlsx` records for it."""
    if not path.exists():
        return {}
    import pandas as pd
    df = pd.read_excel(path, sheet_name="verdicts")
    out = {}
    for r in df.itertuples():
        out[int(r.original_id)] = {
            "kashshaf_id": int(r.kashshaf_id) if pd.notna(r.kashshaf_id) else None,
            "final_title": r.final_title if isinstance(r.final_title, str) else None,
            "keep": r.keep if isinstance(r.keep, str) else "",
            "verdict": r.verdict, "master_ids": r.master_ids if isinstance(r.master_ids, str) else ""}
    return out


def load_authors(path: Path) -> dict[int, dict]:
    """Ahl al-Bayt contributor id -> Kashshaf author (`ab-authors.xlsx`): the
    master's author_id and its canonical name from authors.xlsx."""
    if not path.exists():
        return {}
    import pandas as pd
    out = {}
    for r in pd.read_excel(path, sheet_name="authors").itertuples():
        if pd.notna(r.kashshaf_author_id):
            out[int(r.ab_contributor_id)] = {"author_id": int(r.kashshaf_author_id),
                                             "name": r.canonical_name if isinstance(r.canonical_name, str) else None}
    return out


def load_genres(path: Path) -> dict[int, int]:
    """original_id -> Kashshaf genre_id (`ab-genres.xlsx`)."""
    if not path.exists():
        return {}
    import pandas as pd
    df = pd.read_excel(path, sheet_name="genres")
    return {int(o): int(g) for o, g in zip(df.original_id, df.genre_id) if pd.notna(g)}


ROLE_GROUP = {
    "author": "authors", "editor": "editors", "research": "editors", "research_comment": "editors",
    "research_edit": "editors", "edit_comment": "editors", "attention": "editors",
    "translation": "translators", "translation_research": "translators",
    "translation_explanation": "translators", "explanation": "commentators", "comment": "commentators",
    "summary": "arrangers", "preparation": "arrangers", "preparation_comment": "arrangers",
    "foreword": "preface_by", "revision": "reviewers", "supervision": "reviewers",
    "transcriber": "transcribers", "transcription": "transcribers", "participation": "contributors",
}
PERSON_GROUPS = ("authors", "original_authors", "attributed_authors", "translators", "commentators",
                 "arrangers", "editors", "reviewers", "transmitters", "preface_by", "transcribers",
                 "digital_encoders", "digital_preparers", "contributors")
YEAR = re.compile(r"\d{3,4}")
MARK = re.compile(r"(?<![ء-ي])(ق|ش|م)(?![ء-ي])")


def year_calendar(s: str, m: re.Match) -> tuple[int, str]:
    """One year in a print_year string and its calendar."""
    digits = m.group()
    y = int(digits)
    if y > 2100 and 1000 <= int(digits[::-1]) <= 2100:
        y = int(digits[::-1])
    if y >= 1800:
        return y, "gregorian"
    after = MARK.search(s[m.end():m.end() + 8])
    before = list(MARK.finditer(s[max(0, m.start() - 6):m.start()]))
    mark = after.group(1) if after else (before[-1].group(1) if before else "ق")
    return y, {"ش": "solar_hijri", "م": "gregorian"}.get(mark, "hijri")


def parse_print_year(raw: str | None) -> list[dict]:
    """`1423 ق`, `1387 ه . ش`, `م / 1997 ق 1417` -> the master's publication.dates entries."""
    if not raw:
        return []
    s = ZERO_WIDTH.sub("", str(raw).translate(EAST_DIGITS))
    years = {}
    for m in YEAR.finditer(s):
        y, calendar = year_calendar(s, m)
        years.setdefault(calendar, str(y))
    hijri = years.get("hijri")
    greg = years.get("gregorian") or (str(int(years["solar_hijri"]) + 621) if "solar_hijri" in years else None)
    return [{"raw": str(raw), "hijri": hijri, "gregorian": greg, "type": "publication_or_edition",
             "source_key": "سنة الطبع"}]


def place_of(publisher: str | None) -> str | None:
    """`دار إحياء التراث العربي - بيروت - لبنان` -> `بيروت - لبنان`."""
    if not publisher or " - " not in publisher:
        return None
    return " - ".join(publisher.split(" - ")[1:]).strip() or None


def entry(value, key):
    return {"value_raw": str(value), "source_key": key}


def build_metadata_json(row: dict, cat: Catalogue, vols: list[int], contributors: list[dict],
                        pub_name: str | None, cat_names: list[str], missing_vols: list[int]) -> dict:
    """The master's normalised bibliographic record, from the library's own fields."""
    first = vols[0]
    b, tr = cat.books[first], cat.title(first)
    people = {g: [] for g in PERSON_GROUPS}
    for c in contributors:
        group = ROLE_GROUP.get(c["role"] or "", "contributors")
        people[group].append({"name_raw": c["display_name"] or c["name"], "role_raw": c["role_ar"],
                              "source_key": f"ahlbayt.{c['role']}"})
    alternate = [entry(t.get("title"), f"book_translations.{lang}")
                 for lang, t in cat.titles.get(first, {}).items() if lang != "ar" and t.get("title")]
    place = place_of(pub_name)
    isbns = sorted({cat.books[v]["isbn"] for v in vols if cat.books[v]["isbn"]})
    notes = [entry(tr["abstract"], "abstract")] if tr.get("abstract") else []
    if missing_vols:
        notes.append(entry(f"volumes missing from the library: {missing_vols}", "ab_missing_volume_numbers"))
    as_str = lambda v: "" if v is None else str(v)
    return {
        "id": as_str(row["id"]), "corpus": "ahlbayt", "subcorpus": "ahlbayt", "family": "ahlbayt",
        "original_id": row["original_id"],
        "row_metadata": {k: as_str(row[k]) for k in
                         ("title", "author_id", "author_name", "death_ah", "century_ah", "genre_id")},
        "titles": {"main": [entry(tr.get("title"), "الكتاب")], "short": [], "alternate": alternate,
                   "row_title": [entry(row["title"], "row.title")], "english": []},
        "responsible_persons": people,
        "publication": {
            "publishers": [entry(pub_name, "الناشر")] if pub_name else [],
            "places": [entry(place, "الناشر")] if place else [],
            "dates": parse_print_year(b.get("print_year")),
            "edition": [entry(b["printing_batch_no"], "الطبعة")] if b.get("printing_batch_no") else [],
            "series": [], "volumes": [entry(len(vols), "عدد الأجزاء")],
            "page_range": [], "page_count_meta": [entry(sum(cat.books[v]["page_count"] or 0 for v in vols), "page_count")],
            "container_titles": []},
        "source_info": {
            "digital_source": [entry("Maktabat Ahl al-Bayt", "source")],
            "repository_or_project": [entry("مكتبة أهل البيت", "library")],
            "manuscript_witnesses": [], "sigla": [], "urls": [], "scrape_urls": [], "doi": [], "uris": [],
            "source_ids": [entry(v, "ab_book_id") for v in vols] +
                          [entry(cat.books[v]["v3_id"], "v3_id") for v in vols if cat.books[v]["v3_id"]] +
                          [entry(i, "ISBN") for i in isbns],
            "collection_ids": []},
        "existing_transliterations": {k: [] for k in (
            "author_lat", "author_shuhra", "author_lat_shuhra", "title_transcription", "title_latin",
            "author_transcription", "author_latin")},
        "classification": {"subjects": [], "genres": [], "genre_english": [], "style": [], "tags": [],
                           "category": [entry(n, "ahlbayt.category") for n in cat_names if n],
                           "legal_school": []},
        "notes": notes, "excluded_as_nonbibliographic": [], "unknown_keys": [],
        "flags": ["AHLBAYT_STRUCTURED_RECORD"],
        "debug": {"book_meta_parse_error": "", "author_meta_parse_error": "",
                  "book_meta_genuinely_unparsed": [], "author_meta_genuinely_unparsed": []},
    }


def build_citation_json(row: dict, people: dict, pub_name: str | None, dates: list[dict], b: dict,
                        n_vols: int) -> dict:
    """The fields the app formats Chicago and MLA citations from. The master
    has them romanised; these are still in Arabic, and say so."""
    names = lambda g: [p["name_raw"] for p in people.get(g, [])]
    date = None
    if dates:
        d = dates[0]
        date = " / ".join(x for x in (f"{d['hijri']} AH" if d["hijri"] else None, d["gregorian"]) if x) or d["raw"]
    return {"title": row["title"], "authors": [row["author_name"]] if row["author_name"] else names("authors"),
            "editors": names("editors"), "translators": names("translators"), "arrangers": names("arrangers"),
            "place": place_of(pub_name), "publisher": pub_name.split(" - ")[0].strip() if pub_name else None,
            "date": date, "edition": b.get("printing_batch_no") or None,
            "volumes": str(n_vols) if n_vols > 1 else None, "warnings": ["not romanized"]}


def work_row(cat: Catalogue, vols: list[int], result: dict | None, decision: dict | None = None,
             authors_map: dict | None = None, genres_map: dict | None = None) -> dict:
    first = vols[0]
    b = cat.books[first]
    tr = cat.title(first)
    authors = cat.authors(first)
    deaths = {cid: cat.death(cid) for cid in authors}
    known = [d.ah for d in deaths.values() if d.ah is not None]
    death_ah = max(known) if known else None
    centuries = [century_of(d) for d in deaths.values() if century_of(d) is not None]
    pub = cat.publishers.get(b["publisher_id"], {})
    pub_name = pub.get("names", {}).get("ar") or next(iter(pub.get("names", {}).values()), None)
    numbers = [cat.books[v]["volume_number"] for v in vols]
    present = {k[0] for k in (volume_key(cat, v) for v in vols) if k}
    missing_vols = sorted(set(range(1, max(present) + 1)) - present) if present else []
    cats = cat.categories.get(first, [])
    cat_names = [cat.category_rows.get(c, {}).get("names", {}).get("ar") for c in cats]

    contributors, seen = [], set()
    for c in (c for v in vols for c in cat.book_contribs[v]):
        if (c["contribution_id"], c["contributor_id"]) in seen:
            continue
        seen.add((c["contribution_id"], c["contributor_id"]))
        p = cat.person(c["contributor_id"])
        contributors.append({
            "role_id": c["contribution_id"], "role": cat.roles.get(c["contribution_id"], {}).get("slug"),
            "role_ar": cat.role_name(c["contribution_id"]), "contributor_id": c["contributor_id"],
            "name": p.get("name"), "display_name": c["display_name"], "era": p.get("era")})
    author_info = []
    for cid in authors:
        p, raw, d = cat.person(cid), cat.contributors.get(cid, {}), deaths[cid]
        author_info.append({
            "contributor_id": cid, "name": p.get("name"), "aliases": json.loads(p["aliases"]) if p.get("aliases") else None,
            "era": p.get("era"), "born_at": ts(raw.get("born_at")), "died_at": ts(raw.get("died_at")),
            "death_ah": d.ah, "century_ah": century_of(d), "death_basis": d.basis})

    book_meta = [f"الكتاب: {tr.get('title')}"]
    for c in contributors:
        label = "المؤلف" if c["role_id"] == AUTHOR_ROLE else (c["role_ar"] or c["role"])
        era = f" (ت {c['era']})" if c["role_id"] == AUTHOR_ROLE and c["era"] else ""
        book_meta.append(f"{label}: {c['display_name'] or c['name']}{era}")
    if pub_name:
        book_meta.append(f"الناشر: {pub_name}")
    for k, label in (("printing_batch_no", "الطبعة"), ("print_year", "عام النشر"), ("isbn", "ISBN")):
        if b.get(k):
            book_meta.append(f"{label}: {b[k]}")
    if tr.get("printery"):
        book_meta.append(f"المطبعة: {tr['printery']}")
    book_meta.append(f"عدد الأجزاء: {len(vols)}")
    if cat_names:
        book_meta.append(f"التصنيف: {'، '.join(n for n in cat_names if n)}")
    book_meta.append("[المصدر: مكتبة أهل البيت]")
    author_meta = []
    for i, a in enumerate(author_info):
        author_meta += [f"id: {a['contributor_id']}", f"name: {a['name']}",
                        f"death_number: {a['death_ah'] if a['death_ah'] is not None else -1}",
                        f"death_text: {a['era'] or ''}", f"is_main_author: {i == 0}"]

    authors_map, genres_map = authors_map or {}, genres_map or {}
    matched = authors_map.get(authors[0]) if authors else None
    author_name = (matched or {}).get("name") or " و".join(a["name"] for a in author_info if a["name"]) or None

    stats = (result or {}).get("stats", {})
    # death_ah is unsigned: pre-Islamic authors are 0, as in the master.
    if death_ah is not None and death_ah < 0:
        death_ah = 0
    # Known only by century: century * 100 (قرن 7 -> 700). A BCE author gets 0.
    if death_ah is None:
        bases = {d.basis for d in deaths.values()}
        if centuries:
            death_ah = max(centuries) * 100
        elif "bce" in bases:
            death_ah, centuries = 0, [1]
    if death_ah == 0 and not centuries:
        centuries = [1]
    decision = decision or {}
    in_corpus = 1 if not decision or decision["keep"].startswith("add") else 0
    row = {
        "id": decision.get("kashshaf_id"), "corpus": "ahlbayt", "title": decision.get("final_title") or tr.get("title"),
        "author_id": (matched or {}).get("author_id"), "author_name": author_name,
        "death_ah": death_ah, "century_ah": max(centuries) if centuries else None,
        "genre_id": genres_map.get(first), "page_count": stats.get("pages"), "token_count": None,
        "paginated": 1, "original_id": str(first),
        "tags": jl([n for n in cat_names if n]), "book_meta": jl(book_meta), "author_meta": jl(author_meta),
        "in_corpus": in_corpus, "parts": len(vols), "metadata_json": None, "citation_json": None,
        "ab_title": tr.get("title"), "ab_dedup_verdict": decision.get("verdict"),
        "ab_dedup_keep": decision.get("keep"), "ab_master_ids": decision.get("master_ids"),
        "ab_book_ids": jl(vols), "ab_volume_numbers": jl(numbers),
        "ab_parts": jl([volume_part(cat, v) for v in vols]),
        "ab_volume_count": len(vols), "ab_missing_volume_numbers": jl(missing_vols),
        "ab_v3_ids": jl([cat.books[v]["v3_id"] for v in vols]),
        "ab_title_translations": jl({k: v.get("title") for k, v in cat.titles.get(first, {}).items()}),
        "ab_volume_labels": jl([cat.title(v).get("volume_label") for v in vols]),
        "ab_abstract": tr.get("abstract"), "ab_printery": tr.get("printery"),
        "ab_isbn": jl([cat.books[v]["isbn"] for v in vols]),
        "ab_print_year": b["print_year"], "ab_printing_batch_no": b["printing_batch_no"],
        "ab_source": b["source"], "ab_status": b["status"],
        "ab_publisher_id": b["publisher_id"], "ab_publisher_name": pub_name,
        "ab_publisher_website": pub.get("website"),
        "ab_languages": jl(cat.languages.get(first, [])),
        "ab_category_ids": jl(cats), "ab_category_names": jl(cat_names),
        "ab_author_ids": jl(authors), "ab_authors": jl(author_info),
        "ab_death_basis": jl(sorted({d.basis for d in deaths.values()})),
        "ab_contributors": jl(contributors),
        "ab_page_count_catalogue": sum(cat.books[v]["page_count"] or 0 for v in vols),
        "ab_created_by_id": b["created_by_id"],
        "ab_created_at": ts(b["created_at"]), "ab_updated_at": ts(b["updated_at"]),
    }
    for k in ("headings", "toc_matched_line", "toc_inserted_top", "toc_inserted_before_line",
              "toc_appended_end", "toc_already_heading", "refs_unwrapped", "hemistichs",
              "zero_width_deleted", "footnotes_dropped", "pages_empty"):
        row[f"ab_stat_{k}"] = stats.get(k, 0)
    row["ab_unknown_nodes"] = jl({k: v for k, v in stats.items() if k.startswith("unknown_node_")})
    row["ab_missing_files"] = jl((result or {}).get("missing", []))
    row["ab_errors"] = jl((result or {}).get("errors", []))
    mj = build_metadata_json(row, cat, vols, contributors, pub_name, cat_names, missing_vols)
    row["metadata_json"] = jl(mj)
    row["citation_json"] = jl(build_citation_json(row, mj["responsible_persons"], pub_name,
                                                  mj["publication"]["dates"], b, len(vols)))
    return row


def volume_rows(cat: Catalogue, works: list[list[int]]) -> list[dict]:
    out = []
    for vols in works:
        for v in vols:
            r = {"original_id": str(vols[0]), "ab_book_id": v}
            r.update({f"book_{k}": val for k, val in cat.books[v].items()})
            for k in ("book_created_at", "book_updated_at", "book_deleted_at"):
                r[k] = ts(r[k])
            for lang, tr in cat.titles.get(v, {}).items():
                for k, val in tr.items():
                    if k not in ("book_id", "language_id"):
                        r[f"{k}_{lang}"] = val
            r["languages"] = jl(cat.languages.get(v, []))
            r["category_ids"] = jl(cat.categories.get(v, []))
            r["contributors"] = jl([{k: c[k] for k in ("contributor_id", "contribution_id", "display_name", "weight")}
                                    for c in cat.book_contribs[v]])
            r["attachments"] = jl([{k: a[k] for k in ("context", "id", "filename", "size", "mimetype", "hash")}
                                   for a in cat.attachments.get(v, [])])
            out.append(r)
    return out


def contributor_rows(cat: Catalogue, works: list[list[int]]) -> list[dict]:
    ids = sorted({c["contributor_id"] for vols in works for v in vols for c in cat.book_contribs[v]})
    out = []
    for cid in ids:
        raw = dict(cat.contributors.get(cid, {"id": cid}))
        for k in ("born_at", "died_at", "created_at", "updated_at", "deleted_at"):
            raw[f"{k}_iso"] = ts(raw.get(k))
        for lang, tr in cat.contributor_tr.get(cid, {}).items():
            for k, val in tr.items():
                if k not in ("contributor_id", "language_id"):
                    raw[f"{k}_{lang}"] = val
        d = cat.death(cid)
        raw.update({"death_ah": d.ah, "century_ah": century_of(d), "death_basis": d.basis})
        out.append(raw)
    return out


def lookup_rows(table: dict) -> list[dict]:
    out = []
    for r in table.values():
        r = dict(r)
        for k in ("names", "bios"):
            for lang, val in r.pop(k, {}).items():
                r[f"{k[:-1]}_{lang}"] = val
        out.append(r)
    return out


EXCEL_CELL_MAX = 32767


def write_metadata(path: Path, sheets: dict[str, list[dict]]) -> None:
    import pandas as pd
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    def safe(v):
        if isinstance(v, str):
            v = ILLEGAL_CHARACTERS_RE.sub("", v)
            if len(v) > EXCEL_CELL_MAX:
                v = v[: EXCEL_CELL_MAX - 20] + " …[truncated]"
        return v

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part.xlsx")
    with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
        for name, data in sheets.items():
            df = pd.DataFrame(data)
            df = df.apply(lambda col: col.map(safe)) if not df.empty else df
            df.to_excel(xw, sheet_name=name, index=False)
    os.replace(tmp, path)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--app-dir", type=Path, default=DEFAULT_APP,
                    help="Ahl al-Bayt library backend folder (default: [sources] ahlbayt in config.toml)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="where the JSON goes (default: data/raw/ahlbayt)")
    ap.add_argument("--metadata", type=Path, default=DEFAULT_METADATA,
                    help="metadata workbook (default: metadata/sources/ahlbayt.xlsx)")
    ap.add_argument("--authors", type=Path, default=DEFAULT_AUTHORS,
                    help="Ahl al-Bayt author -> Kashshaf author_id and canonical name "
                         "(default: metadata/ab-authors.xlsx; skipped if absent)")
    ap.add_argument("--genres", type=Path, default=DEFAULT_GENRES,
                    help="work -> Kashshaf genre_id (default: metadata/ab-genres.xlsx; skipped if absent)")
    ap.add_argument("--dedup", type=Path, default=DEFAULT_DEDUP,
                    help="comparison with the master corpus: Kashshaf ids, titles, what is added "
                         "(default: metadata/ab-dedup.xlsx; skipped if absent)")
    ap.add_argument("--languages", nargs="+", default=["ar"],
                    help="book languages to take (default: ar)")
    ap.add_argument("--all-languages", action="store_true", help="take every language")
    ap.add_argument("--max-death-ah", type=int, default=1348,
                    help="latest author death year, AH, inclusive (default: 1348)")
    ap.add_argument("--no-date-filter", action="store_true",
                    help="take books whatever their authors' dates")
    ap.add_argument("--include-footnotes", action="store_true",
                    help="keep footnotes in each page's `footnote` field")
    ap.add_argument("--include-unpublished", action="store_true",
                    help="also take draft and unpublished books (deleted ones never)")
    ap.add_argument("--ids", nargs="+", type=int, help="only these Ahl al-Bayt book ids")
    ap.add_argument("--force", action="store_true", help="rewrite JSON that already exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="select, group and write the workbook; extract no text")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="parallel extraction processes (default: half the cores)")
    args = ap.parse_args()

    if args.app_dir is None:
        sys.exit("no Ahl al-Bayt app folder: set [sources] ahlbayt in config.toml, or pass --app-dir")
    book_dir = args.app_dir / "book"
    t0 = time.time()
    cat = Catalogue(args.app_dir / "db" / "app.db")
    selected, excluded = select_books(cat, args, book_dir)
    works = group_volumes(cat, selected)
    multi = sum(1 for w in works if len(w) > 1)
    print(f"catalogue: {len(cat.books)} books | selected {len(selected)} books "
          f"-> {len(works)} works ({multi} multi-volume) | excluded {len(excluded)}")
    for reason, n in Counter(e["reason"] for e in excluded).most_common():
        print(f"  excluded  {n:6d}  {reason}")

    results: dict[str, dict] = {}
    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        tmp_dir = tempfile.mkdtemp(prefix="ab_extract_")
        specs = []
        for vols in works:
            oid = str(vols[0])
            out_path = args.out / f"{oid}.json"
            spec = {"original_id": oid, "title": cat.title(vols[0]).get("title"),
                    "volumes": [{"book_id": v, "part": volume_part(cat, v)} for v in vols],
                    "book_dir": str(book_dir), "tmp_dir": tmp_dir, "out_path": str(out_path),
                    "include_footnotes": args.include_footnotes}
            specs.append(spec)
        todo = [s for s in specs if args.force or not Path(s["out_path"]).exists()]
        print(f"extracting {len(todo)} works ({len(specs) - len(todo)} already written) "
              f"with {args.workers} workers")
        done = 0
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(extract_work, s): s["original_id"] for s in todo}
            for fut in as_completed(futures):
                oid = futures[fut]
                try:
                    results[oid] = fut.result()
                except Exception as e:
                    results[oid] = {"stats": {}, "errors": [f"worker: {e}"], "missing": [],
                                    "paginated": False, "written": False}
                done += 1
                if done % 250 == 0 or done == len(todo):
                    print(f"  {done}/{len(todo)}  {time.time() - t0:.0f}s")
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        # A regrouping folds old works into others; their files must not linger.
        if not args.ids:
            current = {f"{s['original_id']}.json" for s in specs}
            stale = [p for p in args.out.glob("*.json") if p.name not in current]
            for p in stale:
                p.unlink()
            if stale:
                print(f"removed {len(stale)} JSON files of works that no longer exist")
        for s in specs:
            if s["original_id"] in results or not Path(s["out_path"]).exists():
                continue
            with open(s["out_path"], encoding="utf-8") as f:
                doc = json.load(f)
            results[s["original_id"]] = {
                "stats": doc.get("ab_stats") or {"pages": sum(len(p["pages"]) for p in doc["parts"])},
                "errors": [], "missing": [], "paginated": doc.get("paginated", False),
                "written": True}

    dedup = load_dedup(args.dedup)
    if dedup:
        unknown = [w[0] for w in works if w[0] not in dedup]
        print(f"dedup:    {args.dedup.name}: {sum(1 for w in works if dedup.get(w[0], {}).get('keep', '').startswith('add'))} "
              f"works added, {sum(1 for w in works if dedup.get(w[0], {}).get('keep', '').startswith('skip'))} already in Kashshaf"
              + (f", {len(unknown)} not in it (written with no id): {unknown[:10]}" if unknown else ""))
    authors_map, genres_map = load_authors(args.authors), load_genres(args.genres)
    if authors_map or genres_map:
        print(f"reconcile: {len(authors_map)} Ahl al-Bayt authors matched to Kashshaf authors, "
              f"{len(genres_map)} works with a genre")
    work_rows = [work_row(cat, vols, results.get(str(vols[0])), dedup.get(vols[0]), authors_map, genres_map)
                 for vols in works]
    sheets = {
        "works": work_rows,
        "volumes": volume_rows(cat, works),
        "contributors": contributor_rows(cat, works),
        "excluded": excluded,
        "publishers": lookup_rows(cat.publishers),
        "categories": lookup_rows(cat.category_rows),
        "roles": lookup_rows(cat.roles),
        "languages": cat.language_rows,
        "run": [{"run_at": datetime.now().isoformat(timespec="seconds"),
                 "app_dir": str(args.app_dir), "languages": "all" if args.all_languages else " ".join(args.languages),
                 "max_death_ah": None if args.no_date_filter else args.max_death_ah,
                 "include_footnotes": args.include_footnotes,
                 "include_unpublished": args.include_unpublished, "dry_run": args.dry_run,
                 "ids": jl(args.ids) if args.ids else None}],
    }
    write_metadata(args.metadata, sheets)

    totals = Counter()
    for r in results.values():
        totals.update(r.get("stats", {}))
    failed = [oid for oid, r in results.items() if r.get("errors") or not r.get("written")]
    print(f"\nworkbook: {args.metadata}")
    if results:
        print(f"written:  {sum(1 for r in results.values() if r.get('written'))} works, "
              f"{totals['pages']} pages, {totals['headings']} headings, "
              f"{totals['toc_matched_line'] + totals['toc_inserted_top'] + totals['toc_inserted_before_line']} contents titles placed")
        print(f"changed:  {totals['refs_unwrapped']} refs unwrapped, {totals['hemistichs']} hemistichs, "
              f"{totals['zero_width_deleted']} zero-width chars, {totals['footnotes_dropped']} footnotes dropped")
        if failed:
            print(f"problems: {len(failed)} works (see ab_errors / ab_missing_files): {failed[:10]}")
    print(f"time:     {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
