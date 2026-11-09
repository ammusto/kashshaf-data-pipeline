#!/usr/bin/env python
"""Process books through the complete data preparation pipeline.
Uses CAMeL Tools BERTUnfactoredDisambiguator for morphological analysis."""
import argparse
import gc
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta
from multiprocessing import Process, Queue
from multiprocessing.pool import Pool
from pathlib import Path
from typing import Optional

import pandas as pd

from settings import DATA, METADATA

sys.stdout.reconfigure(encoding='utf-8')

PAGESTART_PREFIX = "PAGESTART"
NEWLINE_MARKER = "NEWLINE"

# BERT's context is 512 wordpieces, and an Arabic word is often more than one.
MAX_CHUNK_TOKENS = 400

# Values above 4 lose to padding overhead; 32 produced data mismatches.
DISAMBIGUATE_GROUP_SIZE = 4

REINIT_EVERY_N_BOOKS = 32

LIKELY_SENTENCE_STARTERS = {
    'وقال', 'فقال', 'وكان', 'فكان', 'ولما', 'فلما',
    'وإن', 'فإن', 'وهو', 'فهو', 'وهي', 'فهي',
    'ثم', 'وأما', 'فأما', 'وقد', 'فقد', 'ولكن', 'فلم',
    'وأخبرنا', 'وحدثنا', 'فأخبرنا', 'فحدثنا',
    'وأنبأنا', 'فأنبأنا', 'وسمعت', 'فسمعت',
    'قال', 'أخبرنا', 'حدثنا', 'أنبأنا', 'سمعت',
}

TAG_RE = re.compile(r"<[^>]+>")

ARABIC_LETTER_RANGES = [
    (0x0600, 0x06FF),
    (0x0750, 0x077F),
    (0x08A0, 0x08FF),
]
TASHKIL_START = 0x064B
TASHKIL_END = 0x065F
SUPERSCRIPT_ALEF = 0x0670

COMBINED_TABLE = str.maketrans({
    **{chr(c): None for c in range(0x064B, 0x0660)},
    "\u0670": None,
    "\u0671": None,
    "أ": "ا",
    "إ": "ا",
    "آ": "ا",
    "ٱ": "ا",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
    "ک": "ك",
    "ی": "ي",
    "ے": "ي",
    "ۀ": "ه",
    "ە": "ه",
    "ۃ": "ة",
    "ٹ": "ت",
    "پ": "ب",
    "چ": "ج",
    "ژ": "ز",
    "گ": "ك",
    "ڤ": "ف",
    "ڨ": "ق",
    "ڭ": "ك",
})

TASHKIL_TABLE = str.maketrans(
    "", "", "".join(chr(c) for c in range(0x064B, 0x0660)) + "\u0670\u0671"
)


def is_arabic_letter(c: str) -> bool:
    cp = ord(c)
    for start, end in ARABIC_LETTER_RANGES:
        if start <= cp <= end:
            if TASHKIL_START <= cp <= TASHKIL_END or cp == SUPERSCRIPT_ALEF:
                return False
            return True
    return False


def is_tashkil(c: str) -> bool:
    cp = ord(c)
    return (TASHKIL_START <= cp <= TASHKIL_END) or cp == SUPERSCRIPT_ALEF


def tokenize_arabic(text: str) -> list[str]:
    """Tokenize text into Arabic words only."""
    tokens: list[str] = []
    current_word: list[str] = []

    for c in text:
        if is_arabic_letter(c):
            current_word.append(c)
        elif is_tashkil(c):
            pass
        else:
            if current_word:
                tokens.append("".join(current_word))
                current_word = []

    if current_word:
        tokens.append("".join(current_word))

    return tokens


def tokenize_part_with_markers(pages: list[dict]) -> tuple[list[str], list[int]]:
    """Tokenize all pages in a part, tracking page ownership and paragraph
    boundaries."""
    tokens: list[str] = []
    page_ids: list[int] = []

    for page in pages:
        page_id = int(page['page_id'])
        body = page.get('body', '')
        text = preprocess_for_camel(body)

        tokens.append(f"{PAGESTART_PREFIX}:{page_id}")
        page_ids.append(page_id)

        if '\n' in text:
            paragraphs = text.split('\n')
            first_para_with_tokens = True

            for para in paragraphs:
                para_tokens = tokenize_arabic(para)
                if para_tokens:
                    if not first_para_with_tokens:
                        tokens.append(NEWLINE_MARKER)
                        page_ids.append(page_id)
                    first_para_with_tokens = False

                    tokens.extend(para_tokens)
                    page_ids.extend([page_id] * len(para_tokens))
        else:
            page_tokens = tokenize_arabic(text)
            tokens.extend(page_tokens)
            page_ids.extend([page_id] * len(page_tokens))

    return tokens, page_ids


def _tokenize_page(args: tuple) -> tuple:
    """Tokenize a single page (parallel worker function)."""
    page_idx, page = args
    page_id = int(page['page_id'])
    body = page.get('body', '')
    text = preprocess_for_camel(body)

    if '\n' not in text:
        return (page_idx, page_id, tokenize_arabic(text))

    out: list[str] = []
    first = True
    for para in text.split('\n'):
        para_tokens = tokenize_arabic(para)
        if para_tokens:
            if not first:
                out.append(NEWLINE_MARKER)
            first = False
            out.extend(para_tokens)
    return (page_idx, page_id, out)


def tokenize_part_with_markers_parallel(
    pages: list[dict], pool: Pool
) -> tuple[list[str], list[int]]:
    """Parallel version of tokenize_part_with_markers using a worker pool."""
    results = pool.map(_tokenize_page, enumerate(pages))

    tokens: list[str] = []
    page_ids: list[int] = []

    for page_idx, page_id, page_tokens in sorted(results, key=lambda x: x[0]):
        tokens.append(f"{PAGESTART_PREFIX}:{page_id}")
        page_ids.append(page_id)
        for tok in page_tokens:
            tokens.append(tok)
            page_ids.append(page_id)

    return tokens, page_ids


try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


def check_gpu():
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            gpu_name = torch.cuda.get_device_name(0)
            gpu_mem = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            return True, f"{gpu_name} ({gpu_mem:.1f} GB)"
        return False, "No CUDA GPU detected"
    except ImportError:
        return False, "PyTorch not installed"


def strip_tashkil(text: str) -> str:
    return text.translate(TASHKIL_TABLE)


def normalize_token_for_search(tok: str) -> str:
    return tok.translate(COMBINED_TABLE)


def clean_html(body: str) -> str:
    return TAG_RE.sub("", body)


UNICODE_PUNCT_SYMBOLS = set(
    '.,،:;!?؟؛«»""\'\'()[]{}⦗⦘﴾﴿/\\–—-_…·•●○◦'
    '۔؍٫٬٭'
    '±×÷=≠<>≤≥∞∑∏√∫∂∇'
    '¡¿†‡§¶©®™°′″‴'
)

LATIN_PATTERN = re.compile(r'[A-Za-z\u00C0-\u024F\u1E00-\u1EFF]')


def strip_punct(text: str) -> str:
    return ''.join(c for c in text if c not in UNICODE_PUNCT_SYMBOLS)


def strip_latin(text: str) -> str:
    return LATIN_PATTERN.sub('', text)


def strip_digits(text: str) -> str:
    return ''.join(c for c in text if not c.isdigit())


def normalize_unicode(text: str) -> str:
    return unicodedata.normalize('NFC', text)


def normalize_alef(text: str) -> str:
    return text.translate(str.maketrans({
        '\u0623': '\u0627',
        '\u0625': '\u0627',
        '\u0622': '\u0627',
        '\u0671': '\u0627',
    }))


def normalize_alef_maksura(text: str) -> str:
    return text.replace('\u0649', '\u064A')


def normalize_teh_marbuta(text: str) -> str:
    return text.replace('\u0629', '\u0647')


def preprocess_for_camel(text: str) -> str:
    """Preprocess text before sending to CAMeL disambiguator."""
    text = clean_html(text)
    text = strip_punct(text)
    text = strip_latin(text)
    text = strip_digits(text)
    text = normalize_unicode(text)
    text = normalize_alef(text)
    text = normalize_alef_maksura(text)
    return text


def dump_json(path: Path, obj: dict) -> None:
    if HAS_ORJSON:
        path.write_bytes(orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS))
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def dump_jsonl_line(obj: dict) -> str:
    if HAS_ORJSON:
        return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode('utf-8')
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# An override replaces only BERT's backoff (noun_prop, root O), never a real analysis.

def normalize_override_key(key: str) -> str:
    """Apply pipeline normalization to an override key so it matches what
    BERT will return as result.word."""
    key = unicodedata.normalize('NFC', key)
    key = key.translate(TASHKIL_TABLE)
    key = normalize_alef(key)
    key = normalize_alef_maksura(key)
    return key


def load_overrides(path: Optional[Path]) -> dict[str, dict]:
    """Load and normalize the morphological override dictionary."""
    if path is None:
        return {}
    
    manual_data = _load_overrides_file(path) if path.exists() else {}
    auto_path = path.with_name(path.stem + '_auto' + path.suffix)
    auto_oc_path = path.with_name(path.stem + '_auto_openclass' + path.suffix)
    auto_data = _load_overrides_file(auto_path) if auto_path.exists() else {}
    auto_oc_data = (
        _load_overrides_file(auto_oc_path) if auto_oc_path.exists() else {}
    )
    
    auto_data = {k: v for k, v in auto_data.items() if not k.startswith('_')}
    auto_oc_data = {
        k: v for k, v in auto_oc_data.items() if not k.startswith('_')
    }
    
    manual_normalized = _normalize_overrides_dict(manual_data, str(path))
    auto_normalized = _normalize_overrides_dict(auto_data, str(auto_path))
    auto_oc_normalized = _normalize_overrides_dict(
        auto_oc_data, str(auto_oc_path)
    )
    
    merged = dict(auto_oc_normalized)
    merged.update(auto_normalized)
    merged.update(manual_normalized)
    
    if auto_data or auto_oc_data:
        n_total_auto = len(auto_normalized) + len(auto_oc_normalized)
        print(f"  Loaded {len(manual_normalized)} manual + "
              f"{len(auto_normalized)} closed-class auto + "
              f"{len(auto_oc_normalized)} open-class auto "
              f"= {len(merged)} total")
    
    return merged

def _load_overrides_file(path: Path) -> dict:
    """Read a JSON file as a dict, return {} on error."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f"Could not parse override file {path}: {e}")
    if not isinstance(data, dict):
        raise ValueError(f"Override file must be a JSON object: {path}")
    return data


def _normalize_overrides_dict(raw: dict, source_label: str) -> dict[str, dict]:
    """Normalize keys and validate one override dict."""
    normalized: dict[str, dict] = {}
    sources_for_key: dict[str, str] = {}
    for src_key, analysis in raw.items():
        if src_key.startswith('_'):
            continue
        if not isinstance(analysis, dict):
            raise ValueError(f"Override for {src_key!r} in {source_label} "
                             f"must be a JSON object")
        for required in ('lex', 'root', 'pos'):
            if required not in analysis:
                raise ValueError(
                    f"Override for {src_key!r} in {source_label} missing "
                    f"required field {required!r}"
                )
        clean_analysis = {k: v for k, v in analysis.items()
                          if not k.startswith('_')}
        norm_key = normalize_override_key(src_key)
        if norm_key in normalized:
            if normalized[norm_key] != clean_analysis:
                other_src = sources_for_key[norm_key]
                raise ValueError(
                    f"Override key collision in {source_label}: source "
                    f"keys {other_src!r} and {src_key!r} both normalize to "
                    f"{norm_key!r} but have different analyses."
                )
            continue
        normalized[norm_key] = clean_analysis
        sources_for_key[norm_key] = src_key
    return normalized


def is_noun_prop_O(analysis: Optional[dict]) -> bool:
    """The BERT backoff condition we use as the override gate."""
    return bool(
        analysis
        and analysis.get('pos') == 'noun_prop'
        and analysis.get('root') == 'O'
    )


def select_analysis(
    result, overrides: dict[str, dict]
) -> tuple[Optional[dict], bool]:
    """Pick the analysis to use for this disambiguated word."""
    if not result.analyses:
        return None, False
    bert_top = result.analyses[0].analysis
    if is_noun_prop_O(bert_top):
        override = overrides.get(result.word)
        if override is not None:
            return override, True
    return bert_top, False


def load_progress(progress_file: Path) -> dict:
    if progress_file.exists():
        try:
            with open(progress_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not load progress file: {e}")
    return {
        'completed': [],
        'in_progress': [],
        'stats': {
            'total_books': 0,
            'completed_books': 0,
            'total_pages': 0,
            'total_tokens': 0,
            'total_overrides_applied': 0,
            'start_time': None,
            'last_update': None,
            'errors': []
        }
    }


def save_progress(progress_file: Path, data: dict) -> None:
    """Atomic write via temp + rename."""
    tmp_file = progress_file.with_suffix('.tmp')
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp_file.replace(progress_file)


def extract_features(analysis: dict) -> list:
    features = []
    stt = analysis.get('stt', 'na')
    if stt == 'd':
        features.append('DEF')
    elif stt == 'i':
        features.append('INDEF')
    elif stt == 'c':
        features.append('CNST')

    num = analysis.get('num', 'na')
    if num == 's':
        features.append('SG')
    elif num == 'd':
        features.append('DU')
    elif num == 'p':
        features.append('PL')

    gen = analysis.get('gen', 'na')
    if gen == 'm':
        features.append('MASC')
    elif gen == 'f':
        features.append('FEM')

    cas = analysis.get('cas', 'na')
    if cas == 'n':
        features.append('NOM')
    elif cas == 'g':
        features.append('GEN')
    elif cas == 'a':
        features.append('ACC')

    return features


def extract_clitics(analysis: dict) -> list:
    clitics = []
    proclitic_display = {
        'wa_conj': 'و', 'fa_conj': 'فـ', 'wa_part': 'و', 'fa_part': 'فـ',
        'bi_prep': 'بـ', 'li_prep': 'لـ', 'ka_prep': 'كـ', 'li_conj': 'لـ',
        'Al_det': 'الـ', 'la_emph': 'لـ', 'la_rc': 'لـ', 'sa_fut': 'سـ',
        'ha_voc': 'ها',
    }

    for i in range(4):
        prc = analysis.get(f'prc{i}', '0')
        if prc and prc != '0' and prc != 'na':
            if prc in proclitic_display:
                clitics.append({'type': prc, 'display': proclitic_display[prc]})

    enc0 = analysis.get('enc0', '0')
    if enc0 and enc0 != '0' and enc0 != 'na':
        enclitic_display = {
            '1s_pron': 'ـيّ', '1p_pron': 'ـنا', '2ms_pron': 'ـكَ',
            '2fs_pron': 'ـكِ', '2mp_pron': 'ـكم', '2fp_pron': 'ـكنّ',
            '3ms_pron': 'ـه', '3fs_pron': 'ـها', '3mp_pron': 'ـهم',
            '3fp_pron': 'ـهنّ', '3d_pron': 'ـهما',
        }
        if enc0 in enclitic_display:
            clitics.append({'type': enc0, 'display': enclitic_display[enc0]})
        else:
            clitics.append({'type': enc0, 'display': enc0})

    return clitics


def is_marker(token: str) -> bool:
    return token == NEWLINE_MARKER or token.startswith(PAGESTART_PREFIX)


def parse_pagestart(token: str) -> Optional[int]:
    if token.startswith(f"{PAGESTART_PREFIX}:"):
        try:
            return int(token.split(':')[1])
        except (IndexError, ValueError):
            return None
    return None


def create_chunks(
    tokens: list[str], page_ids: list[int]
) -> list[tuple[list[str], list[int]]]:
    """Create chunks from tokens respecting newline boundaries and BERT's
    context window."""
    if not tokens:
        return []

    chunks: list[tuple[list[str], list[int]]] = []
    current_chunk_tokens: list[str] = []
    current_chunk_page_ids: list[int] = []
    real_token_count = 0

    i = 0
    while i < len(tokens):
        token = tokens[i]
        page_id = page_ids[i]

        if token == NEWLINE_MARKER:
            if current_chunk_tokens and real_token_count > 0:
                chunks.append((current_chunk_tokens, current_chunk_page_ids))
            current_chunk_tokens = []
            current_chunk_page_ids = []
            real_token_count = 0
            i += 1
            continue

        if token.startswith(PAGESTART_PREFIX):
            current_chunk_tokens.append(token)
            current_chunk_page_ids.append(page_id)
            i += 1
            continue

        if real_token_count >= MAX_CHUNK_TOKENS:
            split_idx = _find_split_point(current_chunk_tokens)

            if split_idx > 0:
                chunks.append((
                    current_chunk_tokens[:split_idx],
                    current_chunk_page_ids[:split_idx]
                ))
                remaining_tokens = current_chunk_tokens[split_idx:]
                remaining_page_ids = current_chunk_page_ids[split_idx:]
                current_chunk_tokens = remaining_tokens
                current_chunk_page_ids = remaining_page_ids
                real_token_count = sum(
                    1 for t in remaining_tokens if not is_marker(t)
                )
            else:
                chunks.append((current_chunk_tokens, current_chunk_page_ids))
                current_chunk_tokens = []
                current_chunk_page_ids = []
                real_token_count = 0

        current_chunk_tokens.append(token)
        current_chunk_page_ids.append(page_id)
        real_token_count += 1
        i += 1

    if current_chunk_tokens and any(
        not is_marker(t) for t in current_chunk_tokens
    ):
        chunks.append((current_chunk_tokens, current_chunk_page_ids))

    return chunks


def _find_split_point(tokens: list[str]) -> int:
    real_token_positions: list[int] = []
    for idx, token in enumerate(tokens):
        if not is_marker(token):
            real_token_positions.append(idx)

    if len(real_token_positions) <= MAX_CHUNK_TOKENS // 2:
        return 0

    half_point = MAX_CHUNK_TOKENS // 2
    search_end_real_idx = min(len(real_token_positions), MAX_CHUNK_TOKENS)

    for real_idx in range(search_end_real_idx - 1, half_point - 1, -1):
        actual_idx = real_token_positions[real_idx]
        token = tokens[actual_idx]
        if token in LIKELY_SENTENCE_STARTERS:
            return actual_idx

    if len(real_token_positions) >= MAX_CHUNK_TOKENS:
        return real_token_positions[MAX_CHUNK_TOKENS - 1] + 1

    return 0


# `single` is one BERT call per chunk, `batched` one per group; the tokens are identical.

CALL_PATTERN_SINGLE = "single"
CALL_PATTERN_BATCHED = "batched"
VALID_CALL_PATTERNS = (CALL_PATTERN_SINGLE, CALL_PATTERN_BATCHED)


def _process_disambig_results(
    disambig_results,
    chunk_page_ids: list[int],
    real_token_indices: list[int],
    page_tokens: dict[int, list[dict]],
    overrides: dict[str, dict],
) -> int:
    """Convert BERT's results for one chunk into token docs in page_tokens.
    Returns the count of overrides applied in this chunk."""
    overrides_applied = 0
    for result_idx, result in enumerate(disambig_results):
        chunk_idx = real_token_indices[result_idx]
        page_id = chunk_page_ids[chunk_idx]
        selected, override_applied = select_analysis(result, overrides)
        if override_applied:
            overrides_applied += 1

        if selected is not None:
            raw_lemma = selected.get('lex', result.word)

            if (override_applied
                    and isinstance(selected.get('features'), list)
                    and isinstance(selected.get('clitics'), list)):
                features = selected['features']
                clitics = selected['clitics']
            else:
                features = extract_features(selected)
                clitics = extract_clitics(selected)

            token_doc = {
                "idx": len(page_tokens[page_id]),
                "surface": result.word,
                "lemma": strip_tashkil(raw_lemma),
                "root": selected.get('root'),
                "pos": selected.get('pos', 'UNK'),
                "features": features,
                "clitics": clitics,
            }
        else:
            token_doc = {
                "idx": len(page_tokens[page_id]),
                "surface": result.word,
                "lemma": strip_tashkil(result.word),
                "root": None,
                "pos": "UNK",
                "features": [],
                "clitics": [],
            }
        page_tokens[page_id].append(token_doc)
    return overrides_applied

def disambiguate_part(
    pages: list[dict],
    disambiguator,
    overrides: dict[str, dict],
    pool: Optional[Pool] = None,
    call_pattern: str = CALL_PATTERN_SINGLE,
) -> tuple[list[dict], int]:
    """Disambiguate a part's pages."""
    if call_pattern not in VALID_CALL_PATTERNS:
        raise ValueError(
            f"call_pattern must be one of {VALID_CALL_PATTERNS}, got {call_pattern!r}"
        )

    if not pages:
        return [], 0

    if pool is not None:
        tokens, page_ids = tokenize_part_with_markers_parallel(pages, pool)
    else:
        tokens, page_ids = tokenize_part_with_markers(pages)

    if not tokens:
        return [], 0

    chunks = create_chunks(tokens, page_ids)

    chunks_real: list[tuple[list[str], list[int], list[str], list[int]]] = []
    for chunk_tokens, chunk_page_ids in chunks:
        real_tokens = []
        real_token_indices = []
        for idx, token in enumerate(chunk_tokens):
            if not is_marker(token):
                real_tokens.append(token)
                real_token_indices.append(idx)
        if real_tokens:
            chunks_real.append(
                (chunk_tokens, chunk_page_ids, real_tokens, real_token_indices)
            )

    page_tokens: dict[int, list[dict]] = {
        int(page['page_id']): [] for page in pages
    }
    overrides_applied = 0

    if call_pattern == CALL_PATTERN_SINGLE:
        for chunk_tokens, chunk_page_ids, real_tokens, real_token_indices in chunks_real:
            disambig_results = disambiguator.disambiguate(real_tokens)
            overrides_applied += _process_disambig_results(
                disambig_results,
                chunk_page_ids,
                real_token_indices,
                page_tokens,
                overrides,
            )

    else:
        for group_start in range(0, len(chunks_real), DISAMBIGUATE_GROUP_SIZE):
            group = chunks_real[group_start:group_start + DISAMBIGUATE_GROUP_SIZE]
            sentences = [c[2] for c in group]

            if not hasattr(disambiguator, 'disambiguate_sentences'):
                raise RuntimeError(
                    "Installed camel_tools version does not expose "
                    "disambiguate_sentences. Use --call-pattern single or upgrade."
                )

            results_per_sentence = disambiguator.disambiguate_sentences(sentences)

            for (chunk_tokens, chunk_page_ids, real_tokens, real_token_indices), \
                    disambig_results in zip(group, results_per_sentence):
                overrides_applied += _process_disambig_results(
                    disambig_results,
                    chunk_page_ids,
                    real_token_indices,
                    page_tokens,
                    overrides,
                )

    page_docs = []
    for page in pages:
        page_id = int(page['page_id'])
        tokens_for_page = page_tokens[page_id]
        if not tokens_for_page:
            continue
        page_docs.append({
            'page_id': page_id,
            'page_number': page.get('page_number', ''),
            'body': page.get('body', ''),
            'tokens': tokens_for_page,
        })

    return page_docs, overrides_applied


def process_book(
    book_path: Path,
    output_base: Path,
    disambiguator,
    overrides: dict[str, dict],
    metadata_lookup: dict[int, dict],
    verbose: bool = False,
    worker_id: Optional[int] = None,
    pool: Optional[Pool] = None,
    call_pattern: str = CALL_PATTERN_SINGLE,
) -> dict:
    with open(book_path, 'r', encoding='utf-8') as f:
        book = json.load(f)
    book_id = int(book['id'])

    meta = metadata_lookup.get(book_id, {})
    if not meta:
        msg = (f"WARNING: book id {book_id} not in metadata Excel, "
               f"falling back to raw JSON fields")
        if worker_id is not None:
            print(f"[W{worker_id}] {msg}")
        else:
            print(msg)

    def excel_field(name: str, json_key: Optional[str] = None,
                    default=None):
        """Excel wins; raw JSON falls back; default if neither has it.
        pandas reads empty cells as NaN, which we treat as missing."""
        if name in meta:
            v = meta[name]
            if v is not None and v == v:
                if not (isinstance(v, str) and not v.strip()):
                    return v
        return book.get(json_key or name, default)

    # The indexes read death_ah from metadata.db, not from this copy.
    title = excel_field('title')

    # Missing, zero or negative (Shamela has -1) -> None.
    raw_death = excel_field('death_ah')
    try:
        death_ah = int(raw_death) if raw_death not in (None, '', 0, '0') \
            else None
    except (ValueError, TypeError):
        death_ah = None
    if death_ah is not None and death_ah < 0:
        death_ah = None

    stats = {
        'book_id': book_id,
        'title': title,
        'pages_processed': 0,
        'tokens_processed': 0,
        'parts_processed': 0,
        'overrides_applied': 0,
    }
    output_base.mkdir(parents=True, exist_ok=True)
    output_file = output_base / f"{book_id}.jsonl"
    all_page_docs = []
    seen_page_keys = set()

    for part_idx, part in enumerate(book.get('parts', [])):
        part_label = part.get('part', '')
        pages = part.get('pages', [])
        if not pages:
            continue
        if verbose:
            print(f"    Part {part_idx}: {len(pages)} pages")
        t0 = time.time()
        page_docs, part_overrides = disambiguate_part(
            pages, disambiguator, overrides, pool, call_pattern
        )
        t_bert = time.time() - t0
        stats['overrides_applied'] += part_overrides
        part_tokens = 0
        for page_doc in page_docs:
            tokens = page_doc['tokens']
            surface_text = " ".join(
                normalize_token_for_search(t["surface"]) for t in tokens
            )
            lemma_text = " ".join(t["lemma"] for t in tokens)
            root_text = " ".join(
                t["root"] for t in tokens if t["root"] is not None
            )
            full_page_doc = {
                "id": book_id,
                "part_index": part_idx,
                "page_id": page_doc["page_id"],
                "death_ah": death_ah,
                "part_label": part_label,
                "page_number": page_doc["page_number"],
                "body": page_doc["body"],
                "surface_text": surface_text,
                "lemma_text": lemma_text,
                "root_text": root_text,
                "tokens": tokens,
            }
            page_key = (part_idx, full_page_doc['page_id'])
            if page_key in seen_page_keys:
                print(
                    f"WARNING: Duplicate page key in book {book_id}: "
                    f"part={page_key[0]}, page={page_key[1]} - skipping"
                )
                continue
            seen_page_keys.add(page_key)
            all_page_docs.append(full_page_doc)
            part_tokens += len(tokens)
        if verbose:
            print(f"      {part_tokens:,} tokens | BERT:{t_bert:.2f}s")
        stats['parts_processed'] += 1
        if worker_id is not None:
            tok_per_sec = part_tokens / t_bert if t_bert > 0 else 0
            total_parts = len(book.get('parts', []))
            running_tokens = sum(len(d['tokens']) for d in all_page_docs)
            print(
                f"  [W{worker_id}] Part {part_idx + 1}/{total_parts}: "
                f"{part_tokens:,} tok @ {tok_per_sec:,.0f} tok/s | "
                f"Total: {running_tokens:,} tok"
            )

    t0 = time.time()
    tmp_file = output_file.with_suffix('.jsonl.tmp')
    with open(tmp_file, 'w', encoding='utf-8') as f:
        for page_doc in all_page_docs:
            f.write(dump_jsonl_line(page_doc) + '\n')
    os.replace(tmp_file, output_file)
    t_write = time.time() - t0
    if verbose:
        print(
            f"    Wrote {len(all_page_docs)} pages to {output_file.name} "
            f"in {t_write:.2f}s"
        )
    stats['pages_processed'] = len(all_page_docs)
    stats['tokens_processed'] = sum(len(d['tokens']) for d in all_page_docs)
    return stats

def _convert_model_to_fp16(disambiguator) -> bool:
    """Find the underlying torch model inside the CAMeL disambiguator and
    convert it to FP16. Returns True if successful, False if no model found."""
    import torch
    
    likely_paths = [
        '_model',
        '_bert_model',
        'model',
        '_tagger._model',
        '_tagger.model',
    ]
    
    def get_path(obj, path):
        cur = obj
        for part in path.split('.'):
            if not hasattr(cur, part):
                return None
            cur = getattr(cur, part)
        return cur
    
    for path in likely_paths:
        candidate = get_path(disambiguator, path)
        if isinstance(candidate, torch.nn.Module):
            print(f"  [FP16] Converting model at disambiguator.{path}")
            candidate.half()
            return True
    
    seen = set()
    found_path = [None]
    
    def search(obj, prefix, depth):
        if id(obj) in seen or depth > 4 or found_path[0] is not None:
            return
        seen.add(id(obj))
        for name in dir(obj):
            if name.startswith('__'):
                continue
            try:
                val = getattr(obj, name)
            except Exception:
                continue
            if isinstance(val, torch.nn.Module):
                found_path[0] = f"{prefix}.{name}"
                val.half()
                return
            if hasattr(val, '__dict__') and not isinstance(
                val, (str, bytes, int, float, bool, list, tuple, dict, set)
            ):
                search(val, f"{prefix}.{name}", depth + 1)
    
    search(disambiguator, "disambiguator", 0)
    if found_path[0]:
        print(f"  [FP16] Converted model found via search: {found_path[0]}")
        return True
    
    print("  [FP16] WARNING: no torch.nn.Module found, FP16 NOT applied")
    return False


def _init_bert(batch_size: int, use_fp16: bool = False):
    import torch
    from camel_tools.disambig.bert import BERTUnfactoredDisambiguator
    d = BERTUnfactoredDisambiguator.pretrained(
        model_name='msa',
        top=1,
        use_gpu=torch.cuda.is_available() and torch.cuda.device_count() > 0,
        batch_size=batch_size,
        ranking_cache_size=0,
        pretrained_cache=False,
        cache_size=100000,
    )
    if use_fp16:
        # Converted now so the first predict() does not pay a CPU-to-GPU transfer.
        bert = d._model['unfactored']._model
        bert.to('cuda').half()
        bert.eval()
        p = next(bert.parameters())
        print(f"  [FP16] BERT converted: device={p.device}, dtype={p.dtype}")
    return d


def _between_books_cleanup():
    """Free CUDA cache and run GC. Cheap; runs between every book."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    gc.collect()


def production_worker(
    worker_id: int,
    book_queue: Queue,
    result_queue: Queue,
    output_base: Path,
    progress_file: Path,
    overrides_path: Optional[Path],
    metadata_excel: Path,
    batch_size: int,
    tokenize_workers: int,
    verbose: bool,
    call_pattern: str = CALL_PATTERN_SINGLE,
    use_fp16: bool = False,
):
    """Worker process: init BERT, load metadata + overrides, process books."""
    import filelock
    fp16_tag = ', FP16' if use_fp16 else ''
    print(f"[Worker {worker_id}] Initializing BERT (GPU{fp16_tag})...")
    disambiguator = _init_bert(batch_size, use_fp16=use_fp16)
    print(f"[Worker {worker_id}] BERT ready")

    print(f"[Worker {worker_id}] Loading metadata from {metadata_excel}")
    df = pd.read_excel(metadata_excel)
    metadata_lookup: dict[int, dict] = {}
    for _, row in df.iterrows():
        try:
            book_id = int(row['id'])
        except (ValueError, TypeError):
            continue
        metadata_lookup[book_id] = row.to_dict()
    print(f"[Worker {worker_id}] Metadata loaded: "
          f"{len(metadata_lookup)} books")

    try:
        overrides = load_overrides(overrides_path)
    except Exception as e:
        print(f"[Worker {worker_id}] FATAL: failed to load overrides: {e}")
        return
    if overrides:
        print(f"[Worker {worker_id}] Loaded {len(overrides)} overrides")
    else:
        print(f"[Worker {worker_id}] No overrides loaded "
              f"(path={overrides_path})")
    tokenize_pool = Pool(tokenize_workers) if tokenize_workers > 1 else None
    lock_file = progress_file.with_suffix('.lock')
    lock = filelock.FileLock(lock_file)
    books_processed = 0
    total_tokens = 0
    try:
        while True:
            try:
                book_path = book_queue.get(timeout=5)
            except Exception:
                if book_queue.empty():
                    break
                continue
            if book_path is None:
                break
            book_name = book_path.name
            start_time = time.time()
            try:
                # Another worker may have finished it.
                with lock:
                    progress = load_progress(progress_file)
                    if book_name in progress.get('completed', []):
                        if verbose:
                            print(
                                f"[Worker {worker_id}] Skipping {book_name} "
                                f"- already completed"
                            )
                        continue
                    if book_name not in progress.get('in_progress', []):
                        progress.setdefault('in_progress', []).append(book_name)
                        save_progress(progress_file, progress)
                print(f"[Worker {worker_id}] Starting: {book_name}")
                stats = process_book(
                    book_path,
                    output_base,
                    disambiguator,
                    overrides,
                    metadata_lookup,
                    verbose=verbose,
                    worker_id=worker_id,
                    pool=tokenize_pool,
                    call_pattern=call_pattern,
                )
                process_time = time.time() - start_time
                books_processed += 1
                total_tokens += stats['tokens_processed']
                _between_books_cleanup()
                if (books_processed % REINIT_EVERY_N_BOOKS == 0
                        and books_processed > 0):
                    print(f"[Worker {worker_id}] Periodic BERT reinit "
                          f"(every {REINIT_EVERY_N_BOOKS} books)...")
                    del disambiguator
                    gc.collect()
                    disambiguator = _init_bert(batch_size, use_fp16=use_fp16)
                with lock:
                    progress = load_progress(progress_file)
                    if book_name in progress.get('in_progress', []):
                        progress['in_progress'].remove(book_name)
                    if book_name not in progress['completed']:
                        progress['completed'].append(book_name)
                        progress['stats']['completed_books'] += 1
                        progress['stats']['total_pages'] += stats['pages_processed']
                        progress['stats']['total_tokens'] += stats['tokens_processed']
                        progress['stats'].setdefault(
                            'total_overrides_applied', 0
                        )
                        progress['stats']['total_overrides_applied'] += \
                            stats['overrides_applied']
                        progress['stats']['last_update'] = datetime.now().isoformat()
                    save_progress(progress_file, progress)
                result_queue.put({
                    'worker_id': worker_id,
                    'book_name': book_name,
                    'success': True,
                    'pages': stats['pages_processed'],
                    'tokens': stats['tokens_processed'],
                    'overrides_applied': stats['overrides_applied'],
                    'time': process_time,
                    'title': stats.get('title', book_name),
                })
            except Exception as e:
                import traceback
                error_msg = str(e)
                with lock:
                    progress = load_progress(progress_file)
                    if book_name in progress.get('in_progress', []):
                        progress['in_progress'].remove(book_name)
                    progress['stats']['errors'].append({
                        'book': book_name,
                        'error': error_msg,
                        'time': datetime.now().isoformat()
                    })
                    save_progress(progress_file, progress)
                result_queue.put({
                    'worker_id': worker_id,
                    'book_name': book_name,
                    'success': False,
                    'error': error_msg,
                })
                if verbose:
                    traceback.print_exc()
    finally:
        if tokenize_pool:
            tokenize_pool.close()
            tokenize_pool.join()
    print(
        f"[Worker {worker_id}] Finished: {books_processed} books, "
        f"{total_tokens:,} tokens"
    )

def find_books_by_date_range(
    raw_dir: Path,
    date_min: int = 0,
    date_max: int = 9999,
    metadata_df: Optional[pd.DataFrame] = None,
) -> list[Path]:
    filtered = metadata_df[
        (metadata_df['death_ah'] >= date_min)
        & (metadata_df['death_ah'] <= date_max)
    ]

    books = []
    for _, row in filtered.iterrows():
        corpus = row['corpus']
        original_id = str(row['original_id'])
        filename = f"{corpus}.{original_id}.json"
        filepath = raw_dir / filename
        if filepath.exists():
            books.append((filepath, int(row['death_ah']), int(row['id'])))

    books.sort(key=lambda x: (x[1], x[2]))
    return [b[0] for b in books]


def get_book_id_from_json(json_file: Path) -> Optional[int]:
    try:
        with open(json_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('id')
    except Exception:
        return None


def find_all_books(raw_dir: Path) -> list[Path]:
    return sorted(raw_dir.glob('*.json'))


def find_books_by_size(
    raw_dir: Path,
    min_kb: float = 0,
    max_kb: float = float('inf'),
    count: Optional[int] = None,
    random_select: bool = False,
) -> list[Path]:
    import random

    candidates = []
    for json_file in raw_dir.glob('*.json'):
        size_kb = json_file.stat().st_size / 1024
        if min_kb <= size_kb <= max_kb:
            candidates.append((json_file, size_kb))

    candidates.sort(key=lambda x: x[1])

    if count is None:
        return [c[0] for c in candidates]

    if random_select and len(candidates) > count:
        selected = random.sample(candidates, count)
        return [c[0] for c in selected]

    return [c[0] for c in candidates[:count]]


def find_book_by_size_target(
    raw_dir: Path, target_kb: float
) -> Optional[Path]:
    best_match = None
    best_diff = float('inf')

    for json_file in raw_dir.glob('*.json'):
        size_kb = json_file.stat().st_size / 1024
        diff = abs(size_kb - target_kb)
        if diff < best_diff:
            best_match = json_file
            best_diff = diff

    return best_match


def process_books_parallel(
    books: list[Path],
    output_base: Path,
    progress_file: Path,
    raw_dir: Path,
    overrides_path: Optional[Path],
    metadata_excel: Path,
    num_processes: int = 6,
    tokenize_workers: int = 2,
    batch_size: int = 512,
    verbose: bool = False,
    call_pattern: str = CALL_PATTERN_SINGLE,
    use_fp16: bool = False,
):
    progress = load_progress(progress_file)
    completed_set = set(progress['completed'])
    in_progress = set(progress.get('in_progress', []))
    stale = in_progress & completed_set
    if stale:
        progress['in_progress'] = [
            b for b in progress['in_progress'] if b not in stale
        ]
        save_progress(progress_file, progress)
        in_progress -= stale
    remaining_books = [b for b in books if b.name not in completed_set]
    for book in books:
        if book.name in in_progress and book not in remaining_books:
            remaining_books.append(book)
    if not remaining_books:
        print("All books already processed!")
        return
    progress['stats']['total_books'] = len(books)
    if not progress['stats']['start_time']:
        progress['stats']['start_time'] = datetime.now().isoformat()
    save_progress(progress_file, progress)
    try:
        preview_overrides = load_overrides(overrides_path)
    except Exception as e:
        print(f"\nERROR loading overrides: {e}")
        return
    print(f"\n{'=' * 70}")
    print("PRODUCTION PROCESSING")
    print(f"{'=' * 70}")
    print(f"Total books:                 {len(books)}")
    print(f"Already completed:           {len(completed_set)}")
    print(f"To process:                  {len(remaining_books)}")
    print(f"BERT processes:              {num_processes}")
    print(f"Tokenize workers per proc:   {tokenize_workers}")
    print(f"Total threads:               "
          f"{num_processes * (tokenize_workers + 1)}")
    print(f"BERT batch_size:             {batch_size}")
    print(f"FP16 inference:              {use_fp16}")
    print(f"Max chunk tokens:            {MAX_CHUNK_TOKENS}")
    print(f"Call pattern:                {call_pattern}")
    if call_pattern == CALL_PATTERN_BATCHED:
        print(f"Disambiguate group size:     {DISAMBIGUATE_GROUP_SIZE}")
    print(f"Reinit cadence:              every {REINIT_EVERY_N_BOOKS} books")
    print(f"Overrides loaded:            {len(preview_overrides)}")
    print(f"Override path:               {overrides_path}")
    print(f"orjson available:            {HAS_ORJSON}")
    print(f"{'=' * 70}\n")
    book_queue = Queue()
    result_queue = Queue()
    for book in remaining_books:
        book_queue.put(book)
    for _ in range(num_processes):
        book_queue.put(None)
    workers = []
    start_time = time.time()
    for worker_id in range(num_processes):
        p = Process(
            target=production_worker,
            args=(
                worker_id,
                book_queue,
                result_queue,
                output_base,
                progress_file,
                overrides_path,
                metadata_excel,
                batch_size,
                tokenize_workers,
                verbose,
                call_pattern,
                use_fp16,
            ),
        )
        p.start()
        workers.append(p)
        print(f"Started worker {worker_id} (PID {p.pid})")
    session_books = 0
    session_tokens = 0
    session_pages = 0
    session_overrides = 0
    total_completed = len(completed_set)
    total_tokens = progress['stats']['total_tokens']
    total_pages = progress['stats']['total_pages']
    total_overrides = progress['stats'].get('total_overrides_applied', 0)
    books_this_session = len(remaining_books)
    try:
        while any(p.is_alive() for p in workers):
            try:
                result = result_queue.get(timeout=1)
                if result['success']:
                    session_books += 1
                    session_tokens += result['tokens']
                    session_pages += result['pages']
                    session_overrides += result.get('overrides_applied', 0)
                    total_completed += 1
                    total_tokens += result['tokens']
                    total_pages += result['pages']
                    total_overrides += result.get('overrides_applied', 0)
                    elapsed = time.time() - start_time
                    tokens_per_sec = (
                        session_tokens / elapsed if elapsed > 0 else 0
                    )
                    pct = (
                        session_books / books_this_session * 100
                        if books_this_session > 0 else 100
                    )
                    remaining = books_this_session - session_books
                    books_per_min = (
                        session_books / elapsed * 60 if elapsed > 0 else 0
                    )
                    eta_sec = (
                        remaining / books_per_min * 60
                        if books_per_min > 0 else 0
                    )
                    eta = str(timedelta(seconds=int(eta_sec)))
                    bar_w = 30
                    filled = (
                        int(bar_w * session_books / books_this_session)
                        if books_this_session > 0 else bar_w
                    )
                    bar = '█' * filled + '░' * (bar_w - filled)
                    title = result.get('title', result['book_name'])[:35]
                    overrides_str = (
                        f"ovr:{result.get('overrides_applied', 0):,}"
                    )
                    print(
                        f"[{bar}] {pct:.1f}% | W{result['worker_id']}: "
                        f"{title}... {result['tokens']:,} tok in "
                        f"{result['time']:.1f}s {overrides_str} | "
                        f"Total: {total_tokens:,} | "
                        f"{tokens_per_sec:,.0f} tok/s | ETA: {eta}"
                    )
                else:
                    print(
                        f"[ERROR] W{result['worker_id']}: "
                        f"{result['book_name']} - {result['error']}"
                    )
            except Exception:
                continue
    except KeyboardInterrupt:
        print("\n\nInterrupted! Waiting for workers to finish current books...")
        while not book_queue.empty():
            try:
                book_queue.get_nowait()
            except Exception:
                break
        for _ in range(num_processes):
            book_queue.put(None)
    for p in workers:
        p.join(timeout=30)
    elapsed = time.time() - start_time
    print(f"\n{'=' * 70}")
    print("PROCESSING COMPLETE")
    print(f"{'=' * 70}")
    print(
        f"Session: {session_books} books, {session_pages:,} pages, "
        f"{session_tokens:,} tokens, {session_overrides:,} overrides applied"
    )
    print(
        f"Total:   {total_completed}/{len(books)} books, "
        f"{total_pages:,} pages, {total_tokens:,} tokens, "
        f"{total_overrides:,} overrides applied"
    )
    print(f"Time:    {elapsed / 60:.1f} min")
    if elapsed > 0:
        print(f"Speed:   {session_tokens / elapsed:,.0f} tok/s")
    print(f"{'=' * 70}")

def main():
    parser = argparse.ArgumentParser(
        description='Process Arabic texts through CAMeL Tools morphological analysis.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python process_batch.py --all
  python process_batch.py --date-min 0 --date-max 100
  python process_batch.py --book data/raw/shamela.1.json
  python process_batch.py --all --processes 6
  python process_batch.py --status
  python process_batch.py --all --reset
""")

    parser.add_argument('--all', action='store_true')
    parser.add_argument('--book', type=str)
    parser.add_argument('--date-min', type=int, default=0)
    parser.add_argument('--date-max', type=int, default=9999)
    parser.add_argument('--count', type=int)
    parser.add_argument('--min-kb', type=float, default=0)
    parser.add_argument('--max-kb', type=float, default=float('inf'))
    parser.add_argument('--size-target', type=float)
    parser.add_argument('--random', action='store_true')

    parser.add_argument('--processes', type=int, default=6,
                        help='Number of BERT processes (default: 6)')
    parser.add_argument('--tokenize-workers', type=int, default=2,
                        help='Tokenization workers per process (default: 2)')
    parser.add_argument('--batch-size', type=int, default=512,
                        help='BERT batch size (default: 512)')
    parser.add_argument(
        '--call-pattern',
        type=str,
        default=CALL_PATTERN_SINGLE,
        choices=VALID_CALL_PATTERNS,
        help=(
            'BERT call pattern: "single" (default, one disambiguate() per chunk) '
            'or "batched" (disambiguate_sentences over groups of '
            f'{DISAMBIGUATE_GROUP_SIZE} chunks). Single is the proven multi-worker '
            'config; batched is faster in single-worker microbenchmarks.'
        ),
    )

    parser.add_argument('--input-dir', type=str, default=str(DATA / 'canonical'))
    parser.add_argument('--output-dir', type=str, default=str(DATA / 'processed'))
    parser.add_argument(
        '--progress-file', type=str, default=str(DATA / 'processing_progress.json')
    )
    parser.add_argument(
        '--metadata',
        type=str,
        default=str(METADATA),
        help='Metadata Excel, used to number the books '
             '(default: metadata/metadata.xlsx)',
    )
    parser.add_argument(
        '--overrides',
        type=str,
        default='morph_overrides.json',
        help='Path to morphology override JSON (default: morph_overrides.json)',
    )

    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--reset', action='store_true')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument(
            '--fp16',
            action='store_true',
            help='Run BERT in FP16 (Ada/Ampere GPUs). ~1.5-2x faster, '
                '~50%% less VRAM. Validate output equivalence first.',
        )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent.parent

    def resolve_path(p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            return (script_dir / path).resolve()
        return path

    raw_dir = resolve_path(args.input_dir)
    output_base = resolve_path(args.output_dir)
    progress_file = resolve_path(args.progress_file)
    overrides_path = resolve_path(args.overrides)
    metadata_excel = resolve_path(args.metadata)
    if not metadata_excel.exists():
        print(f"FATAL: metadata Excel not found at {metadata_excel}")
        return

    if args.status:
        if progress_file.exists():
            progress = load_progress(progress_file)
            stats = progress['stats']
            print(f"Progress: {stats['completed_books']}/"
                  f"{stats['total_books']} books")
            print(f"Pages: {stats['total_pages']:,}")
            print(f"Tokens: {stats['total_tokens']:,}")
            print(f"Overrides applied: "
                  f"{stats.get('total_overrides_applied', 0):,}")
            print(f"Errors: {len(stats['errors'])}")
            print(f"In-progress: {len(progress.get('in_progress', []))}")
            if stats['start_time']:
                print(f"Started: {stats['start_time']}")
            if stats['last_update']:
                print(f"Last update: {stats['last_update']}")
            if progress.get('in_progress'):
                print("\nIn-progress (will retry):")
                for b in progress['in_progress'][:5]:
                    print(f"  {b}")
            if stats['errors']:
                print("\nRecent errors:")
                for err in stats['errors'][-5:]:
                    print(f"  {err['book']}: {err['error'][:60]}")
        else:
            print("No progress file found")
        return

    if args.reset and progress_file.exists():
        progress_file.unlink()
        lock_file = progress_file.with_suffix('.lock')
        if lock_file.exists():
            lock_file.unlink()
        print("Progress reset")

    try:
        validated_overrides = load_overrides(overrides_path)
        if validated_overrides:
            print(f"Validated {len(validated_overrides)} overrides "
                  f"from {overrides_path}")
        elif overrides_path.exists():
            print(f"Override file exists but contains no entries: "
                  f"{overrides_path}")
        else:
            print(f"No override file at {overrides_path} "
                  f"(overrides disabled)")
    except Exception as e:
        print(f"\nERROR: failed to validate overrides: {e}")
        return

    if args.book:
        books = [Path(args.book)]
        if not books[0].exists():
            print(f"Book not found: {args.book}")
            return
    elif args.size_target:
        book = find_book_by_size_target(raw_dir, args.size_target)
        if book:
            size_kb = book.stat().st_size / 1024
            print(f"Found book: {book.name} ({size_kb:.1f} KB)")
            books = [book]
        else:
            print(f"No book found near {args.size_target} KB")
            return
    elif args.all:
        books = find_all_books(raw_dir)
        print(f"Found {len(books)} books")
    elif args.date_min > 0 or args.date_max < 9999:
        books = find_books_by_date_range(
            raw_dir, args.date_min, args.date_max,
            metadata_df=pd.read_excel(metadata_excel),
        )
        print(f"Found {len(books)} books in date range "
              f"{args.date_min}-{args.date_max}")
    elif args.count or args.min_kb > 0 or args.max_kb < float('inf'):
        books = find_books_by_size(
            raw_dir, args.min_kb, args.max_kb, args.count, args.random
        )
        print(f"Selected {len(books)} books")
    else:
        parser.print_help()
        return

    if not books:
        print("No books found!")
        return

    total_mb = sum(b.stat().st_size for b in books) / (1024 * 1024)
    print(f"Total size: {total_mb:.1f} MB")

    if args.verbose and len(books) <= 20:
        print("\nBooks:")
        for b in books:
            size_kb = b.stat().st_size / 1024
            print(f"  {b.name} ({size_kb:.1f} KB)")

    if args.dry_run:
        print("\n--- DRY RUN ---")
        if progress_file.exists():
            progress = load_progress(progress_file)
            completed_set = set(progress['completed'])
            remaining = [b for b in books if b.name not in completed_set]
            print(f"Would skip: {len(books) - len(remaining)} completed")
            print(f"Would process: {len(remaining)} books")
        else:
            print(f"Would process: {len(books)} books")
        print(f"Settings: {args.processes} procs, chunk {MAX_CHUNK_TOKENS}, "
              f"pattern={args.call_pattern}, "
              f"reinit every {REINIT_EVERY_N_BOOKS}")
        print(f"Overrides: {len(validated_overrides)} from {overrides_path}")
        return

    output_base.mkdir(parents=True, exist_ok=True)

    has_gpu, gpu_note = check_gpu()
    if not has_gpu:
        print(f"{gpu_note}: running on the CPU in one process. "
              f"This suits a few books, and is very slow on a large corpus.")
        args.processes = 1
        args.fp16 = False

    print(f"\n{'=' * 60}")
    print(
        f"Starting batch processing at "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    print(
        f"Books: {len(books)} | Processes: {args.processes} | "
        f"Chunk: {MAX_CHUNK_TOKENS} | Pattern: {args.call_pattern}"
    )
    print(f"{'=' * 60}\n")

    try:
        import filelock  # noqa: F401
    except ImportError:
        print("ERROR: filelock package required. "
              "Install with: pip install filelock")
        return

    process_books_parallel(
        books=books,
        output_base=output_base,
        progress_file=progress_file,
        raw_dir=raw_dir,
        overrides_path=overrides_path,
        metadata_excel=metadata_excel,
        num_processes=args.processes,
        tokenize_workers=args.tokenize_workers,
        batch_size=args.batch_size,
        verbose=args.verbose,
        call_pattern=args.call_pattern,
        use_fp16=args.fp16,
    )


def test_newline_insertion():
    """Sanity check: NEWLINE markers correctly inserted between paragraphs."""
    pages = [{
        'page_id': 1,
        'body': 'أحمد بن محمد\nعن أبيه\nعن جده',
    }]
    tokens, _ = tokenize_part_with_markers(pages)
    nl_count = tokens.count(NEWLINE_MARKER)
    assert nl_count == 2, f"Expected 2 NEWLINE markers, got {nl_count}"
    assert tokens[0].startswith(PAGESTART_PREFIX), (
        "First token should be PAGESTART"
    )
    assert tokens[-1] != NEWLINE_MARKER, (
        "Last token should not be NEWLINE_MARKER"
    )
    print("NEWLINE marker insertion test passed")


if __name__ == "__main__":
    test_newline_insertion()
    main()