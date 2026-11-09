#!/usr/bin/env python
"""Kashshaf canonical JSON pipeline."""

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from body_clean import clean_body_v2  # noqa: E402
from settings import DATA, METADATA  # noqa: E402
from nusus_tei import TeiError, tei_parts  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")


log_filename = f"kashshaf_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_filename, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


HTML_TAG_PATTERN = re.compile(r"<(?!/?title\b)[^>]+>")
# re.ASCII keeps \w and \b off Arabic letters.
OPENITI_PATTERNS = [
    re.compile(r"\bAUTO\b", re.ASCII),
    re.compile(r"\bPageV\w*P?\d*\b", re.ASCII),
    re.compile(r"\bQUR\b", re.ASCII),
    re.compile(r"\bCHECK\b", re.ASCII),
    re.compile(r"\bNUM\b", re.ASCII),
    re.compile(r"\bHAD\b", re.ASCII),
    re.compile(r"#\$?\w+", re.ASCII),
    re.compile(r"\bms\d+\b", re.ASCII),
]
POETRY_HEMISTICH = re.compile(r"%~%")
POETRY_PERCENT = re.compile(r"(?<!%)%(?!~)(?!%)")
URL_PATTERN = re.compile(r'https?://[^\s<>"]+')
FILE_REF_PATTERN = re.compile(
    r"\b\w+\.(png|jpg|jpeg|gif|pdf|html)\b", re.IGNORECASE | re.ASCII
)
BRACKETED_LATIN = re.compile(r"\[[A-Za-z\s]+\]")
PIPE_PATTERN = re.compile(r"\|{2,}")
MULTI_SPACE = re.compile(r"[^\S\n]{2,}")
ORPHAN_DIACRITICS = re.compile(
    r"(?<![\u0621-\u064A\u0671-\u06D3\u0640])[\u064B-\u065F\u0670]+"
)
SPACE_BEFORE_PUNCT = re.compile(r"[^\S\n]+([.،؛:؟!])")
BLANK_LINES = re.compile(r"\n\s*\n+")


def normalize_body(body: str) -> str:
    """Corpus-agnostic cleanup, safe for both OpenITI and Shamela."""
    if not body:
        return body
    body = ORPHAN_DIACRITICS.sub("", body)
    body = MULTI_SPACE.sub(" ", body)
    body = SPACE_BEFORE_PUNCT.sub(r"\1", body)
    body = BLANK_LINES.sub("\n", body)
    return body.strip()


def clean_body(body: str) -> str:
    """OpenITI marker removal. Every deletion inserts a space so adjacent
    words are never joined; normalize_body collapses the extras."""
    if not body:
        return body
    body = HTML_TAG_PATTERN.sub(" ", body)
    body = URL_PATTERN.sub(" ", body)
    body = FILE_REF_PATTERN.sub(" ", body)
    for p in OPENITI_PATTERNS:
        body = p.sub(" ", body)
    body = POETRY_HEMISTICH.sub(" ۞ ", body)
    body = POETRY_PERCENT.sub(" ", body)
    body = BRACKETED_LATIN.sub(" ", body)
    body = PIPE_PATTERN.sub(" ", body)
    return normalize_body(body)


def _apply_to_bodies(data: Dict[str, Any], fn) -> Dict[str, Any]:
    for part in data.get("parts", []):
        for page in part.get("pages", []):
            body = page.get("body", "")
            if body:
                page["body"] = fn(body)
    return data


def clean_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return _apply_to_bodies(data, clean_body)


def clean_data_v2(data: Dict[str, Any]) -> Dict[str, Any]:
    return _apply_to_bodies(data, clean_body_v2)


CLEANER = "v2"


def apply_cleaner(data: Dict[str, Any], corpus_is_openiti: bool) -> Dict[str, Any]:
    """v2 cleans every corpus; v1 only ever cleaned OpenITI."""
    if CLEANER == "v2":
        return clean_data_v2(data)
    return clean_data(data) if corpus_is_openiti else data


def normalize_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return _apply_to_bodies(data, normalize_body)


LONG_PAGE_THRESHOLD = 2000
ENDNOTES_THRESHOLD = 4000
TARGET_TOKENS = 400
MIN_TOKENS = 300
MAX_TOKENS = 500

MALFORMED_MARKERS = [
    r"----NO PAGE NO------",
    r"PageV\d+P\d+[ab]?",
    r"PageVW\d+P\d+[A-Za-z]?",
    r"PageV\d+MP\d+",
    r"PageVMSP\d+[ab]?",
    r"MS_PAGE\s*\d*",
    r"PAGE\|[^\n]*",
]
SPLIT_PATTERN = "|".join(f"({p})" for p in MALFORMED_MARKERS)


def count_tokens(text: str) -> int:
    return len(text.split()) if text else 0


def has_malformed_markers(body: str) -> bool:
    return any(re.search(p, body) for p in MALFORMED_MARKERS)


def split_on_markers(body: str) -> List[str]:
    parts = re.split(SPLIT_PATTERN, body)
    out = []
    for part in parts:
        if part is None:
            continue
        s = part.strip()
        if not s:
            continue
        if any(re.fullmatch(p, s) for p in MALFORMED_MARKERS):
            continue
        out.append(s)
    return out


def split_by_tokens(body: str) -> List[str]:
    pages = []
    current, current_tokens = [], 0
    for line in body.split("\n"):
        line_tokens = len(line.split())
        if current_tokens + line_tokens > MAX_TOKENS and current_tokens >= MIN_TOKENS:
            pages.append("\n".join(current))
            current, current_tokens = [line], line_tokens
        elif current_tokens >= TARGET_TOKENS and line_tokens > 0:
            pages.append("\n".join(current))
            current, current_tokens = [line], line_tokens
        else:
            current.append(line)
            current_tokens += line_tokens
    if current:
        last = "\n".join(current)
        if last.strip():
            pages.append(last)
    return pages


def remove_endnotes_pages(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    removed = []
    for part_idx, part in enumerate(data.get("parts", [])):
        pages = part.get("pages", [])
        total_pages = len(pages)
        new_pages = []
        for page_idx, page in enumerate(pages):
            body = page.get("body", "")
            tc = count_tokens(body)
            if tc >= ENDNOTES_THRESHOLD and body.strip().startswith("#####ENDNOTES#"):
                removed.append(
                    {
                        "is_last_page": page_idx == total_pages - 1,
                        "total_pages": total_pages,
                    }
                )
            else:
                new_pages.append(page)
        part["pages"] = new_pages
    return removed


def fix_long_pages(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    splits = []
    for part in data.get("parts", []):
        pages = part.get("pages", [])
        total_pages = len(pages)
        new_pages = []
        page_id_counter = 1
        for page_idx, page in enumerate(pages):
            body = page.get("body", "")
            tc = count_tokens(body)
            is_last = page_idx == total_pages - 1
            if tc > LONG_PAGE_THRESHOLD:
                if has_malformed_markers(body):
                    bodies = split_on_markers(body)
                    final = []
                    for sb in bodies:
                        if count_tokens(sb) > LONG_PAGE_THRESHOLD:
                            final.extend(split_by_tokens(sb))
                        else:
                            final.append(sb)
                    bodies = final
                else:
                    bodies = split_by_tokens(body)
                splits.append(
                    {
                        "is_last_page": is_last,
                        "total_pages": total_pages,
                    }
                )
                for nb in bodies:
                    if nb.strip():
                        new_pages.append(
                            {
                                "page_id": str(page_id_counter),
                                "page_number": str(page_id_counter),
                                "body": nb,
                            }
                        )
                        page_id_counter += 1
            else:
                page["page_id"] = str(page_id_counter)
                page["page_number"] = str(page_id_counter)
                new_pages.append(page)
                page_id_counter += 1
        part["pages"] = new_pages
    return splits


def renumber_pages(data: Dict[str, Any]) -> Dict[str, Any]:
    counter = 1
    for part in data.get("parts", []):
        for page in part.get("pages", []):
            page["page_id"] = str(counter)
            counter += 1
    return data


def apply_fix_pages(data: Dict[str, Any]) -> Dict[str, Any]:
    """Run the fix_pages stage. paginated stays True only if every modification
    was on a last-page in a part with > 10 pages."""
    removed = remove_endnotes_pages(data)
    splits = fix_long_pages(data)
    modifications = removed + splits
    if modifications:
        renumber_pages(data)
        if data.get("paginated") is True:
            all_protected = all(
                m.get("is_last_page") is True and m.get("total_pages", 0) > 10
                for m in modifications
            )
            if not all_protected:
                data["paginated"] = False
    return data


def parse_book_meta(text: str) -> Dict[str, str]:
    book_meta = {}
    misc_counter = 1
    for line in text.split("\n"):
        line = line.strip()
        if not line.startswith("#META#"):
            continue
        if line == "#META#Header#End#":
            continue
        content = line[6:].strip()
        content = re.sub(r"^\d+\.", "", content).strip()
        if "::" in content:
            parts = content.split("::", 1)
            key = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
        elif ":" in content:
            parts = content.split(":", 1)
            key = parts[0].strip()
            value = parts[1].strip() if len(parts) > 1 else ""
        else:
            key = f"misc_info_{misc_counter}"
            value = content
            misc_counter += 1
        if value.upper() not in ["NODATA", "NOTGIVEN", "NOCODE", ""]:
            book_meta[key] = value
    return book_meta


def process_text(text: str) -> str:
    if "#META#Header#End#" in text:
        text = text.split("#META#Header#End#", 1)[1]
    else:
        lines = text.split("\n")
        for i, line in enumerate(lines):
            if not line.startswith("#META#") and line.strip() != "######OpenITI#":
                text = "\n".join(lines[i:])
                break

    text = re.sub(r"###\s*\$", "# ", text)
    text = re.sub(r"(#{1,3})\s*(\|+)", lambda m: f"# {m.group(2)}", text)

    lines = text.split("\n")
    out = []
    header_id = 1
    parent_stack = [0]
    for line in lines:
        m = re.match(r"^#\s+(\|+)\s*(.*?)$", line.strip())
        if m:
            header_text = m.group(2).strip()
            if header_text:
                pipes = m.group(1)
                level = len(pipes) - 1
                while len(parent_stack) > level + 1:
                    parent_stack.pop()
                parent_id = parent_stack[-1] if parent_stack else 0
                out.append(
                    f"<title id={header_id} parent={parent_id}>{header_text}</title>"
                )
                if len(parent_stack) <= level + 1:
                    parent_stack.append(header_id)
                else:
                    parent_stack[level + 1] = header_id
                header_id += 1
        else:
            out.append(line)
    text = "\n".join(out)

    out = []
    for line in text.split("\n"):
        if re.match(r"^#\s+(?!\|)", line):
            out.append(re.sub(r"^#\s+", "", line))
        else:
            out.append(line)
    text = "\n".join(out)

    text = re.sub(r"PageV(\d+)P(\d+)", r"V\1P\2", text)
    text = re.sub(r"~~\s*\n?", " ", text)
    text = re.sub(r"\bms\s*\d+\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"  +", " ", text)
    text = re.sub(r"\s+([.،])", r"\1", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def organize_by_volumes(text: str) -> Dict[str, List[Dict[str, Any]]]:
    markers = list(re.finditer(r"V(\d+)P(\d+)", text))
    if not markers:
        return {"1": [{"page_id": "1", "page_number": "", "body": text}]}

    volumes = defaultdict(list)
    pos = 0
    counter = 1
    for m in markers:
        vol_num = m.group(1)
        page_num = m.group(2)
        content = text[pos : m.start()].strip()
        if content:
            volumes[vol_num].append(
                {
                    "page_id": str(counter),
                    "page_number": str(int(page_num)),
                    "body": content,
                }
            )
            counter += 1
        pos = m.end()
    if pos < len(text):
        remaining = text[pos:].strip()
        if remaining and markers:
            volumes[markers[-1].group(1)].append(
                {
                    "page_id": str(counter),
                    "page_number": "",
                    "body": remaining,
                }
            )
    return dict(volumes)


def detect_pagination(text: str) -> bool:
    return bool(re.search(r"V\d+P\d+", text))


def chunk_text_by_titles(text: str, target_words: int = 300) -> List[Dict[str, Any]]:
    chunks = []
    current, current_wc = [], 0
    page_id = 1
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            current.append(line)
            continue
        if "<title" in line and "</title>" in line:
            current.append(line)
            continue
        wc = len(line.split())
        if current_wc + wc > target_words and current_wc > 50:
            if current:
                chunks.append(
                    {
                        "page_id": str(page_id),
                        "page_number": "",
                        "body": "\n".join(current).strip(),
                    }
                )
                page_id += 1
                current, current_wc = [], 0
        current.append(line)
        current_wc += wc
    if current:
        body = "\n".join(current).strip()
        if body:
            chunks.append(
                {
                    "page_id": str(page_id),
                    "page_number": "",
                    "body": body,
                }
            )
    if not chunks:
        chunks = [{"page_id": "1", "page_number": "", "body": text.strip()}]
    return chunks


def cell_str(row: pd.Series, name: str) -> str:
    """Read a cell as stripped string, treating nan/None as empty."""
    v = row.get(name, "")
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none"):
        return ""
    return s


def cell_int(row: pd.Series, name: str) -> Optional[int]:
    """Read a cell as int. Empty/missing → None."""
    s = cell_str(row, name)
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def cell_bool(row: pd.Series, name: str) -> bool:
    """Read a 0/1 cell as bool."""
    s = cell_str(row, name)
    return s in ("1", "1.0", "true", "True", "TRUE")


def parse_json_list(cell: str) -> List[str]:
    """Parse a JSON-encoded list-of-strings cell. Empty → []."""
    if not cell:
        return []
    try:
        v = json.loads(cell)
        if isinstance(v, list):
            return [str(x) for x in v]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def build_canonical(
    row: pd.Series, parts: List[Dict[str, Any]], paginated: bool
) -> Dict[str, Any]:
    """Build the canonical kashshaf JSON for one book."""
    return {
        "id": cell_int(row, "id"),
        "corpus": cell_str(row, "corpus"),
        "original_id": cell_str(row, "original_id"),
        "title": cell_str(row, "title"),
        "paginated": paginated,
        "token_count": cell_int(row, "token_count"),
        "tags": parse_json_list(cell_str(row, "tags")),
        "book_meta": parse_json_list(cell_str(row, "book_meta")),
        "author_meta": parse_json_list(cell_str(row, "author_meta")),
        "cleaner_version": 1 if CLEANER == "v1" else 2,
        "parts": parts,
    }


def extract_uri_from_filename(filename: str) -> str:
    for ext in [".completed", ".inprogress", ".mARkdown"]:
        if filename.endswith(ext):
            return filename[: -len(ext)]
    if filename.endswith(".json"):
        return filename[:-5]
    return filename


def convert_openiti_file(
    file_path: Path, row: pd.Series, do_clean: bool, do_fix_pages: bool
) -> Optional[Dict[str, Any]]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.error(f"Cannot read {file_path.name}: {e}")
        return None

    processed = process_text(content)
    paginated = detect_pagination(processed)

    if paginated:
        volumes = organize_by_volumes(processed)
        parts = [
            {"part": str(int(v)), "pages": volumes[v]}
            for v in sorted(volumes.keys(), key=int)
        ]
        if not parts:
            parts = [
                {
                    "part": "1",
                    "pages": [{"page_id": "1", "page_number": "", "body": processed}],
                }
            ]
    else:
        parts = [{"part": "", "pages": chunk_text_by_titles(processed)}]

    result = build_canonical(row, parts, paginated)

    # fix_pages must see the raw markers that clean removes.
    if do_fix_pages:
        apply_fix_pages(result)
    if do_clean:
        apply_cleaner(result, corpus_is_openiti=True)

    return result


def convert_shamela_file(file_path: Path, row: pd.Series) -> Optional[Dict[str, Any]]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error(f"Cannot read {file_path.name}: {e}")
        return None

    parts = raw.get('parts', [])
    paginated = cell_bool(row, 'paginated')
    data = build_canonical(row, parts, paginated)
    if CLEANER == "v2":
        return clean_data_v2(data)
    return normalize_data(data)


def convert_nusus_file(file_path: Path, row: pd.Series) -> Optional[Dict[str, Any]]:
    try:
        parts = tei_parts(file_path)
    except (ET.ParseError, TeiError) as e:
        logger.error(f"Cannot convert {file_path.name}: {e}")
        return None
    data = build_canonical(row, parts, cell_bool(row, "paginated"))
    return apply_cleaner(data, corpus_is_openiti=False)


class Pipeline:
    def __init__(
        self,
        openiti_dir: Path,
        shamela_dir: Path,
        output_dir: Path,
        metadata_path: Path,
        do_clean: bool,
        do_fix_pages: bool,
        ahlbayt_dir: Path = Path("data/raw/ahlbayt"),
        corpora: tuple = ("openiti", "shamela", "ahlbayt", "nusus"),
        nusus_dir: Path = Path("data/raw/nusus"),
    ):
        self.openiti_dir = openiti_dir
        self.shamela_dir = shamela_dir
        self.ahlbayt_dir = ahlbayt_dir
        self.nusus_dir = nusus_dir
        self.corpora = corpora
        self.output_dir = output_dir
        self.do_clean = do_clean
        self.do_fix_pages = do_fix_pages
        self.output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"OpenITI dir: {self.openiti_dir.absolute()}")
        logger.info(f"Shamela dir: {self.shamela_dir.absolute()}")
        logger.info(f"Ahl al-Bayt dir: {self.ahlbayt_dir.absolute()}")
        logger.info(f"Nusus dir:   {self.nusus_dir.absolute()}")
        logger.info(f"Corpora:     {' '.join(self.corpora)}")
        logger.info(f"Output dir:  {self.output_dir.absolute()}")
        logger.info(f"Metadata:    {metadata_path.absolute()}")
        logger.info(f"OpenITI options: clean={do_clean}, fix_pages={do_fix_pages}")
        logger.info(f"Log: {log_filename}")

        self.metadata: Dict[str, Dict[str, pd.Series]] = {"openiti": {}, "shamela": {}}
        try:
            df = pd.read_excel(metadata_path, dtype=str, keep_default_na=False)
            required_cols = {"id", "corpus", "original_id", "in_corpus"}
            missing = required_cols - set(df.columns)
            if missing:
                raise ValueError(f"metadata.xlsx missing columns: {missing}")
            included = 0
            excluded = 0
            for _, row in df.iterrows():
                if cell_str(row, "in_corpus") != "1":
                    excluded += 1
                    continue
                corpus = cell_str(row, "corpus")
                original_id = cell_str(row, "original_id")
                if not corpus or not original_id:
                    continue
                if corpus not in self.metadata:
                    self.metadata[corpus] = {}
                self.metadata[corpus][original_id] = row
                included += 1
            logger.info(
                f"Loaded metadata: {included} included, {excluded} excluded (in_corpus != 1)"
            )
            for corpus, rows in self.metadata.items():
                logger.info(f"  {corpus}: {len(rows)} rows")
        except Exception as e:
            logger.error(f"Failed to load metadata: {e}")
            raise

    def _output_path(self, corpus: str, original_id: str) -> Path:
        return self.output_dir / f"{corpus}.{original_id}.json"

    def _write(self, result: Dict[str, Any]) -> bool:
        corpus = result.get("corpus", "")
        original_id = result.get("original_id", "")
        if not corpus or not original_id:
            logger.error(f"Cannot write: missing corpus or original_id in result")
            return False
        out_path = self._output_path(corpus, original_id)
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"Cannot write {out_path.name}: {e}")
            return False

    def run_openiti(self) -> tuple:
        if not self.openiti_dir.is_dir():
            logger.warning(f"OpenITI dir not found, skipping: {self.openiti_dir}")
            return 0, 0, 0
        files = [f for f in self.openiti_dir.glob("*") if f.is_file() and not f.name.startswith(".")]
        logger.info(f"OpenITI: found {len(files)} source files")

        rows = self.metadata.get("openiti", {})
        success = errors = skipped = 0
        for i, fp in enumerate(files, 1):
            stem = extract_uri_from_filename(fp.name)
            row = rows.get(stem)
            if row is None:
                logger.warning(
                    f"[{i}/{len(files)}] No metadata row for {stem}, skipping"
                )
                skipped += 1
                continue
            logger.info(f"[{i}/{len(files)}] {fp.name}")
            result = convert_openiti_file(fp, row, self.do_clean, self.do_fix_pages)
            if result is None:
                errors += 1
                continue
            if self._write(result):
                success += 1
            else:
                errors += 1
        return success, errors, skipped

    def run_shamela(self) -> tuple:
        return self.run_json_corpus("shamela", self.shamela_dir)

    def run_ahlbayt(self) -> tuple:
        # ab_extractor writes the Shamela shape.
        return self.run_json_corpus("ahlbayt", self.ahlbayt_dir)

    def run_nusus(self) -> tuple:
        if not self.nusus_dir.is_dir():
            logger.warning(f"nusus dir not found, skipping: {self.nusus_dir}")
            return 0, 0, 0
        # <text_id>.xml, unpadded; original_id is the same number to six digits
        files = sorted(self.nusus_dir.glob("*.xml"), key=lambda f: (len(f.stem), f.stem))
        logger.info(f"nusus: found {len(files)} source files")

        rows = self.metadata.get("nusus", {})
        success = errors = skipped = 0
        for i, fp in enumerate(files, 1):
            original_id = fp.stem.zfill(6)
            row = rows.get(original_id)
            if row is None:
                logger.warning(
                    f"[{i}/{len(files)}] No metadata row for {original_id}, skipping"
                )
                skipped += 1
                continue
            logger.info(f"[{i}/{len(files)}] {fp.name}")
            result = convert_nusus_file(fp, row)
            if result is None:
                errors += 1
                continue
            if self._write(result):
                success += 1
            else:
                errors += 1
        return success, errors, skipped

    def run_json_corpus(self, corpus: str, src_dir: Path) -> tuple:
        if not src_dir.is_dir():
            logger.warning(f"{corpus} dir not found, skipping: {src_dir}")
            return 0, 0, 0
        files = [f for f in src_dir.glob("*.json") if f.is_file()]
        logger.info(f"{corpus}: found {len(files)} source files")

        rows = self.metadata.get(corpus, {})
        success = errors = skipped = 0
        for i, fp in enumerate(files, 1):
            stem = fp.stem
            row = rows.get(stem)
            if row is None:
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    inner_id = str(raw.get("book_id", "")).strip()
                    if inner_id:
                        row = rows.get(inner_id)
                        if row is not None:
                            stem = inner_id
                except Exception:
                    pass
            if row is None:
                logger.warning(
                    f"[{i}/{len(files)}] No metadata row for {stem}, skipping"
                )
                skipped += 1
                continue
            logger.info(f"[{i}/{len(files)}] {fp.name}")
            result = convert_shamela_file(fp, row)
            if result is None:
                errors += 1
                continue
            if self._write(result):
                success += 1
            else:
                errors += 1
        return success, errors, skipped

    def run(self):
        logger.info("=" * 60)
        logger.info("STARTING KASHSHAF PIPELINE")
        logger.info("=" * 60)

        runners = {"openiti": self.run_openiti, "shamela": self.run_shamela,
                   "ahlbayt": self.run_ahlbayt, "nusus": self.run_nusus}
        totals = {c: runners[c]() for c in self.corpora}

        logger.info("=" * 60)
        logger.info("DONE")
        print(f"\nDone.")
        for corpus, (succ, err, skip) in totals.items():
            logger.info(f"{corpus}: {succ} ok, {err} errors, {skip} skipped")
            print(f"  {corpus}: {succ} ok, {err} errors, {skip} skipped")
        logger.info(f"Output: {self.output_dir.absolute()}")
        logger.info(f"Log:    {log_filename}")
        print(f"  Output:  {self.output_dir.absolute()}")
        print(f"  Log:     {log_filename}")


def main():
    parser = argparse.ArgumentParser(description="Kashshaf canonical JSON pipeline")
    parser.add_argument(
        "--openiti-dir",
        default=str(DATA / "raw/openiti"),
        help="OpenITI raw mARkdown (default: data/raw/openiti)",
    )
    parser.add_argument(
        "--shamela-dir",
        default=str(DATA / "raw/shamela"),
        help="Shamela raw JSONs (default: data/raw/shamela)",
    )
    parser.add_argument(
        "--ahlbayt-dir",
        default=str(DATA / "raw/ahlbayt"),
        help="Ahl al-Bayt library JSONs from extract/ab_extractor.py (default: data/raw/ahlbayt)",
    )
    parser.add_argument(
        "--nusus-dir",
        default=str(DATA / "raw/nusus"),
        help="Nusus TEI, <text_id>.xml (default: data/raw/nusus)",
    )
    parser.add_argument(
        "--corpora",
        nargs="+",
        choices=["openiti", "shamela", "ahlbayt", "nusus"],
        default=["openiti", "shamela", "ahlbayt", "nusus"],
        help="Corpora to convert (default: all four)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DATA / "canonical"),
        help="Output directory (default: data/canonical)",
    )
    parser.add_argument(
        "--metadata",
        default=str(METADATA),
        help="The master metadata (default: metadata/metadata.xlsx)",
    )
    parser.add_argument(
        "--no-clean", action="store_true", help="Skip OpenITI body cleaning"
    )
    parser.add_argument(
        "--cleaner",
        choices=["v1", "v2"],
        default="v2",
        help="Body cleaner: v1 is the OpenITI-only pass the shipped corpus used, "
        "v2 is body_clean.clean_body_v2 over every corpus (default: v2)",
    )
    parser.add_argument(
        "--fix-pages", action="store_true", help="Run OpenITI page-fixing stage"
    )
    args = parser.parse_args()

    global CLEANER
    CLEANER = args.cleaner
    logger.info(f"Body cleaner: {CLEANER}")

    Pipeline(
        openiti_dir=Path(args.openiti_dir),
        shamela_dir=Path(args.shamela_dir),
        output_dir=Path(args.output_dir),
        metadata_path=Path(args.metadata),
        do_clean=not args.no_clean,
        do_fix_pages=args.fix_pages,
        ahlbayt_dir=Path(args.ahlbayt_dir),
        corpora=tuple(args.corpora),
        nusus_dir=Path(args.nusus_dir),
    ).run()


if __name__ == "__main__":
    main()
