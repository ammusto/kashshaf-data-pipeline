#!/usr/bin/env python3
"""Body cleaning, version 2: run on every page before morphology."""
import html
import re

__all__ = ["clean_body_v2", "CLEANER_VERSION"]

CLEANER_VERSION = 2

TITLE_OPEN = re.compile(r"<title\s+id=\d+\s+parent=\d+\s*>")
TITLE_CLOSE = re.compile(r"</title\s*>")
_PH_OPEN, _PH_CLOSE = "", ""

SPACE_PATTERNS = [
    ("openiti_at_marker", re.compile(r"@{1,2}(?:HAD|QUR|NUM|AUTO|CHECK|MS)?\d*", re.I)),
    ("openiti_bare_control", re.compile(r"(?<![A-Za-z])(?:HAD\d*|QUR\d*|QB|QE|AUTO|CHECK|NUM\d*|MS\d+)(?![A-Za-z])")),
    ("hash_marker", re.compile(r"#\d+#")),
    ("hash_word", re.compile(r"#\$?[A-Za-z]\w*", re.ASCII)),
    ("book_chapter_marker", re.compile(r"\[(?:book|chapter|part|vol|volume)\s+[^\]]{0,60}\]", re.I)),
    ("md_image", re.compile(r"!\[[^\]\n؀-ۿ]*\]\([^)\n؀-ۿ]*\)")),
    ("md_image_orphan", re.compile(r"!\[\]\((?=\s|$)")),
    ("file_ref", re.compile(r"[A-Za-z0-9_\-./\\]*\.(?:png|jpe?g|gif|pdf|html?|tiff?|jp2|djvu|xml|txt)\b", re.I)),
    ("url", re.compile(r"(?:https?|inr|ftp)://\S+|www\.\S+", re.I)),
    ("page_marker", re.compile(r"\bPage[VW]\w*P\w*|\bMS_PAGE\w*", re.I)),
    ("kraken_local", re.compile(r"\b(?:kraken|local)\b", re.I)),
    ("editorial_words", re.compile(r"\b(?:hadeeth|Note|hunayn)\b")),
    ("equals_rule", re.compile(r"={3,}")),
    ("footnote_rule", re.compile(r"¬?_{3,}")),
    ("underscore", re.compile(r"_+")),
    ("pipe_runs", re.compile(r"\|{2,}")),
    ("bracketed_latin", re.compile(r"\[[A-Za-z][A-Za-z0-9 ,.'\-]*\]")),
]

FOOTNOTE_ANCHOR = re.compile(r"\(¬\s*([\d٠-٩]+)\)")
STRAY_NOT_SIGN = re.compile(r"¬")

# A space, not nothing: the source used these where a word ended.
INVISIBLES = re.compile(r"[​-‏‪-‮⁦-⁩﻿   ]")

# Not `<` and `>`: the display layer strips anything in angle brackets.
ENTITY = re.compile(r"&(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);")
_ENTITY_MAP = {"&lt;": "‹", "&gt;": "›"}
DOUBLE_ENT = re.compile(r"&lt;&lt;|&gt;&gt;")


def _decode_entity(m: re.Match) -> str:
    e = m.group(0)
    return _ENTITY_MAP.get(e, html.unescape(e))


def _double_quote(m: re.Match) -> str:
    return "«" if m.group(0) == "&lt;&lt;" else "»"


# Only Latin-named tags are markup; an Arabic-named one (<رمز>) holds a word.
BR_TAG = re.compile(r"<br\s*/?>", re.I)
HTML_TAG = re.compile(r"</?[A-Za-z!][^<>]{0,200}>")

LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'’.\-]*")
LATIN_DENSITY_GUARD = 0.25

# Skip-class characters: `أيضا-ابن` would otherwise be the one token `أيضاابن`.
ARABIC = r"؀-ۿݐ-ݿࢠ-ࣿ"
JOINING_SEPARATOR = re.compile(f"(?<=[{ARABIC}])\\s*[-–—·]+\\s*(?=[{ARABIC}])")

MULTI_SPACE = re.compile(r"[ \t]{2,}")
SPACE_AROUND_NL = re.compile(r"[ \t]*\n[ \t]*")
MULTI_NL = re.compile(r"\n{3,}")


def latin_density(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters)


def clean_body_v2(body: str) -> str:
    """Clean one page body before morphology. Corpus-agnostic."""
    if not body:
        return body

    titles = TITLE_OPEN.findall(body)
    body = TITLE_OPEN.sub(_PH_OPEN, body)
    body = TITLE_CLOSE.sub(_PH_CLOSE, body)

    # Doubled first, so `&lt;&lt;` is a quotation mark and not two brackets.
    if "&" in body:
        body = DOUBLE_ENT.sub(_double_quote, body)
        body = ENTITY.sub(_decode_entity, body)

    body = BR_TAG.sub("\n", body)
    body = HTML_TAG.sub("", body)

    body = FOOTNOTE_ANCHOR.sub(r"(\1)", body)

    for _name, rx in SPACE_PATTERNS:
        body = rx.sub(" ", body)
    body = STRAY_NOT_SIGN.sub(" ", body)

    if latin_density(body) <= LATIN_DENSITY_GUARD:
        body = LATIN_RUN.sub(" ", body)

    body = INVISIBLES.sub(" ", body)

    body = JOINING_SEPARATOR.sub(" ", body)

    body = SPACE_AROUND_NL.sub("\n", body)
    body = MULTI_SPACE.sub(" ", body)
    body = MULTI_NL.sub("\n\n", body)
    body = body.strip()

    if titles:
        it = iter(titles)
        body = re.sub(_PH_OPEN, lambda m: next(it), body)
    body = body.replace(_PH_CLOSE, "</title>").replace(_PH_OPEN, "")
    return body
