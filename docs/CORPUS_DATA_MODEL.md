# Corpus data model

This document describes what each stage of the pipeline reads and writes,
and every change it makes to the text along the way. The README explains how
to run the stages. The lists of cleaning and normalisation steps below are
complete, so a change to a cleaner should be recorded here as well.

```
 data/raw/openiti   data/raw/shamela   data/raw/ahlbayt   data/raw/nusus
 (mARkdown)         (JSON)             (JSON)             (TEI)
      │                  │                  │                  │
      │      collect_metadata.py: metadata/sources/*.xlsx, metadata.xlsx   §2
      │                  │                  │                  │
      └──────── kashshaf_pipeline.py: convert, then clean ─────┘
                              │
                              ▼
                 data/canonical/<corpus>.<original_id>.json        §3
                              │
                 process_batch.py: tokenise, CAMeL Tools morphology
                              ▼
                 data/processed/<book_id>.jsonl, one line per page §4
                              │
          ┌───────────────────┼─────────────────────┬────────────────┐
          ▼                   ▼                     ▼                ▼
   build_sqlite_tokens   kashshaf-indexer     kashshaf-boundary   build_toc
   corpus.db  §5         tantivy_index/  §7   boundary_index/ §8  toc.db  §9
   metadata.db §6
```

Every stage after morphology reads `data/processed/` directly. The outputs of
a build are written to `data/build/<version>/`, and nothing in that folder is
ever used as input.

## 1. Sources

| corpus | arrives as | produced by |
|---|---|---|
| `openiti` | mARkdown, one file per version URI | [OpenITI](https://openiti.org); `extract/fetch_openiti.py` downloads by URI |
| `shamela` | JSON with `parts`, `pages` and `body` | `extract/shamela/`, from an installed Shamela library |
| `ahlbayt` | JSON in the same shape as Shamela | `extract/ab_extractor.py`, from the Maktabat Ahl al-Bayt desktop app |
| `nusus` | TEI, one `<text_id>.xml` per text | [nusus](https://github.com/ammusto/nusus) |

## 2. Metadata

### 2.1 Source tables

`scripts/collect_metadata.py` writes one table per source to
`metadata/sources/`, with one row for each raw file. Each table records what
its source says about a book, and nothing is corrected or merged at this
stage. The tables share these columns:

| column | content |
|---|---|
| `corpus`, `original_id` | the source and the book's identifier in it |
| `title`, `author_name`, `death_ah` | as the source gives them |
| `author_key` | the source's own identifier for the author (see §2.3) |
| `category` | the source's subject heading, where it has one |
| `paginated` | whether the source carries printed page numbers |
| `tags`, `book_meta`, `author_meta` | JSON lists of the source's free-text fields |

Each table then adds what is particular to its source. OpenITI rows come
from the OpenITI release metadata, a TSV published on Zenodo, and add the
edition information (`ed_info`) and token count. Shamela rows come from each
JSON header and add co-authors, the author biography, the print flag and PDF
links. The Ahl al-Bayt table is written by the extractor itself and keeps the
library's full record of volumes, contributors and publishers. Nusus rows
come from the TEI header and `text_contrib.csv`. The TEI carries no death
date, so nusus rows leave `death_ah` blank.

### 2.2 The master: `metadata/metadata.xlsx`

The build reads one spreadsheet, with one row per book. It is the only place
a book receives its Kashshaf `id`, its author and genre ids, and its
`in_corpus` flag. The other tables are evidence for it, not inputs to the
build.

| column | required for an in-corpus row | content |
|---|---|---|
| `id` | yes | a whole number, unique and permanent, since citations use it |
| `corpus`, `original_id` | yes | which raw file the row describes |
| `in_corpus` | yes | `1` to build the book, `0` to keep the row and skip the book |
| `title` | yes | the title shown in the app |
| `author_id`, `author_name` | yes | one name for each `author_id` |
| `paginated` | yes | `1` if `page_number` is a printed page number |
| `genre_id`, `genre` | no | fill both or neither; one name for each `genre_id` |
| `death_ah`, `century_ah` | no | the author's death year (AH) and century; a blank century is worked out from the year |
| `tags`, `book_meta`, `author_meta` | no | JSON lists, kept as they are |
| `metadata_json`, `citation_json` | no | a structured record and a citation, when there is one |
| `title_original`, `author_original` | no | the source's own title and author name |
| `page_count`, `token_count`, `parts` | no | recounted by every build |

There is no separate authors or genres file. The `authors` and `genres`
tables in `metadata.db` are built from the distinct pairs in these columns,
and the build stops if an id appears with two different names.

This spreadsheet is the source of every fact about a book, so a change here
never requires morphology to run again. The search indices hold one of these
facts, the death date, which sets the order of results (§7).

### 2.3 How new rows are added

`collect_metadata.py` never changes a row that already exists. For each raw
file without a row, it appends one with the next free `id`, `in_corpus = 1`,
and the source's title, author name and death date. The author is matched to
an existing `author_id` in this order:

1. by the source's own author key, which is the author part of an OpenITI
   URI, the Shamela author id, the Ahl al-Bayt contributor id, or the author
   name in a nusus TEI header;
2. by an exact match on the author's name;
3. if neither matches, by a new `author_id`.

An OpenITI text that the release metadata does not list takes its title and
author from its URI, so every new row can be built without editing.

Genre is left blank, because no source maps cleanly onto a genre list. In
the Kashshaf corpus, 35 of Shamela's 40 categories are spread across more
than one genre. A corpus without genres builds normally, and its books have
no genre in `metadata.db`. Duplicates between sources are also left to you: set
`in_corpus` to `0` on the copy you do not want.

## 3. Canonical JSON

`kashshaf_pipeline.py` converts every raw file whose row has
`in_corpus = 1` into `data/canonical/<corpus>.<original_id>.json`:

```json
{"id": 11, "corpus": "openiti", "original_id": "0001HarithIbnHilliza...", "title": "...",
 "paginated": true, "token_count": null, "tags": [], "book_meta": [], "author_meta": [],
 "cleaner_version": 2,
 "parts": [{"part": "1", "pages": [{"page_id": "1", "page_number": "3", "body": "..."}]}]}
```

The header fields come from the master. `page_id` counts pages from 1 across
the whole book, while `page_number` is the printed number, kept for display
and citation. Every later stage identifies a page by `(part_index, page_id)`,
because some books restart `page_id` in each part.

### 3.1 OpenITI

1. Everything up to `#META#Header#End#` is dropped.
2. A heading line `# |` becomes `<title id=N parent=M>…</title>`. `id` counts
   from 1 within the book and `parent` is the enclosing heading's id, or 0.
   The number of pipes gives the depth. Other `# ` paragraph markers are
   removed and the paragraph is kept.
3. `PageV<vol>P<page>` splits the text into pages. The volume becomes the part
   and the page becomes `page_number`. A text without these markers is
   unpaginated, and is cut into pages of about 300 words at line breaks.
4. `~~` line continuations become a space and `ms<digits>` milestones are
   deleted. Runs of spaces collapse, a space before `.` or `،` is removed, and
   blank lines collapse into one.

OpenITI regenerates its `ms` milestones between releases, so downloading a
text again can give the same words with different page boundaries.

### 3.2 Shamela and Ahl al-Bayt

These files already have pages and `<title id=N parent=M>` headings, written
by their extractors. Their `parts` are copied unchanged before cleaning.

### 3.3 Nusus

The TEI body contains only `pb`, `p`, `div type="chapter"` and `head`. Each
`<pb n=…>` starts a new page whose `page_number` is `n`, even across chapter
boundaries. Each `<p>` becomes one line with its whitespace collapsed, and
empty paragraphs are dropped. A `<head>` becomes `<title id=N parent=0>…</title>`,
where N is the chapter's `n`. Every text is a single part. Any other element
in the body is treated as an error rather than guessed at.

### 3.4 The cleaner

`body_clean.clean_body_v2` runs on every page of every source, before
morphology. It is the only step that changes the text a reader sees.
Headings are set aside first and restored unchanged at the end. The steps,
in order:

| step | what | pattern | becomes |
|---|---|---|---|
| 1 | doubled entities used as quotes | `&lt;&lt;` `&gt;&gt;` | `«` `»` |
| 2 | HTML entities | `&name;` `&#N;` `&#xN;` | decoded, except `&lt;` and `&gt;`, which become `‹` and `›` |
| 3 | line breaks | `<br>` `<br/>` | a newline |
| 4 | tags with Latin names | `</?[A-Za-z!]…>`, up to 200 characters | deleted; tags with Arabic names such as `<رمز>` hold a word and stay |
| 5 | footnote anchors | `(¬١)` | `(١)` |
| 6 | OpenITI `@` markers | `@HAD12` `@@QUR` `@NUM3` `@AUTO` `@CHECK` `@MS…` | a space |
| 6 | bare control words | `HAD1` `QUR` `QB` `QE` `AUTO` `CHECK` `NUM2` `MS12`, outside a Latin word | a space |
| 6 | hash markers | `#12#`, `#word`, `#$word` | a space |
| 6 | editorial brackets | `[book …]` `[chapter …]` `[part …]` `[vol …]` `[volume …]` | a space |
| 6 | Markdown images | `![…](…)` with no Arabic inside, and an orphan `![](` | a space |
| 6 | file references | names ending in `.png`, `.jpg`, `.gif`, `.pdf`, `.htm`, `.tif`, `.jp2`, `.djvu`, `.xml`, `.txt` and similar | a space |
| 6 | URLs | `http(s)://…`, `inr://…`, `ftp://…`, `www.…` | a space |
| 6 | page markers | `PageV…P…`, `PageW…P…`, `MS_PAGE…` | a space |
| 6 | OCR and editor words | `kraken` `local` `hadeeth` `Note` `hunayn` | a space |
| 6 | rules | three or more `=` or `_`, with or without `¬`; any run of `_`; two or more `\|` | a space |
| 6 | bracketed Latin | `[Latin text]` | a space |
| 6 | stray `¬` | `¬` | a space |
| 7 | Latin words | `[A-Za-z][A-Za-z0-9'’.-]*` | a space, unless Latin is more than a quarter of the page's letters (a bibliography) |
| 8 | invisible characters | U+200B to U+200F, U+202A to U+202E, U+2066 to U+2069, U+FEFF, U+00A0, U+2007, U+202F | a space |
| 9 | joining dashes | runs of `-`, `–`, `—` or `·` between two Arabic letters | a space, so `أيضا-ابن` becomes two words |
| 10 | whitespace | spaces around newlines, two or more spaces, three or more newlines | removed, one space, two newlines; then the page is trimmed |

The cleaner does not apply a Unicode normalisation form, fold hamza or alef
forms, remove diacritics, change Arabic punctuation, or touch the
presentation-form ligatures (U+FB50 to U+FDFF and U+FE70 to U+FEFF). The
stored `body` keeps its diacritics and its spelling.

## 4. Morphology: `process_batch.py`

Each canonical page becomes one line of `data/processed/<book_id>.jsonl`:

```json
{"id": 1, "part_index": 0, "page_id": 1, "death_ah": 412, "part_label": "1", "page_number": "55",
 "body": "...",
 "surface_text": "...", "lemma_text": "...", "root_text": "...",
 "tokens": [{"idx": 0, "surface": "كرز", "lemma": "كرز", "root": "ك.ر.ز", "pos": "noun",
             "features": ["INDEF", "SG", "MASC", "NOM"],
             "clitics": [{"type": "wa_conj", "display": "و"}]}]}
```

`body` is the canonical body, unchanged. Every other text field is derived
from a working copy of it, prepared as described below. The JSONL holds
nothing about the book beyond its `id` and a copy of `death_ah`, which is empty
when the death date is missing, zero or negative. The indices do not read that
copy.

### 4.1 Text sent to the tagger

These steps are applied to the working copy, in order:

| step | what | detail |
|---|---|---|
| 1 | tags | `<…>` deleted, headings included |
| 2 | punctuation and symbols | deleted: `. , ، : ; ! ? ؟ ؛ « » " ' ( ) [ ] { } ⦗ ⦘ ﴾ ﴿ / \ – — - _ … · • ● ○ ◦ ۔ ؍ ٫ ٬ ٭ ± × ÷ = ≠ < > ≤ ≥ ∞ ∑ ∏ √ ∫ ∂ ∇ ¡ ¿ † ‡ § ¶ © ® ™ ° ′ ″ ‴` |
| 3 | Latin letters | `A-Z`, `a-z`, U+00C0 to U+024F and U+1E00 to U+1EFF deleted |
| 4 | digits | any character Python counts as a digit, in every script, deleted |
| 5 | Unicode | NFC |
| 6 | alef | `أ` `إ` `آ` `ٱ` become `ا` |
| 7 | alef maqsura | `ى` becomes `ي` |

A word is then a run of characters in the Arabic blocks (U+0600 to U+06FF,
U+0750 to U+077F, U+08A0 to U+08FF). Short vowels and other tashkil inside a
word are dropped, tatweel is kept, and any other character ends the word.
The number of words found this way is the page's token count in every later
stage.

### 4.2 Tagging

Each page is split into paragraphs at line breaks, and each paragraph into
chunks of at most 400 words. Where possible a chunk is cut at a word that
often starts a sentence, such as `قال`, `حدثنا` or `ثم`. The MSA model of
CAMeL Tools' `BERTUnfactoredDisambiguator` analyses each chunk, and the top
analysis of each word is kept. The exception is the tagger's fallback guess
(`pos = noun_prop` with root `O`): if `morph_overrides.json` or
`morph_overrides_auto.json` has an entry for that exact word, the entry's
analysis is used instead.

| field | value |
|---|---|
| `surface` | the word as tokenised in §4.1 |
| `lemma` | the analysis's lemma without diacritics, or the surface when there is no analysis |
| `root` | the analysis's root, letters joined by `.`, or `null` |
| `pos` | CAMeL's part of speech (`noun`, `verb`, `noun_prop`, `prep` and so on), or `UNK` |
| `features` | state, number, gender and case: `DEF`, `INDEF` or `CNST`; `SG`, `DU` or `PL`; `MASC` or `FEM`; `NOM`, `GEN` or `ACC` |
| `clitics` | one `{type, display}` for each proclitic and enclitic, displayed in Arabic |

### 4.3 Search fields

`surface_text`, `lemma_text` and `root_text` join the tokens with single
spaces for indexing. `surface_text` also passes each surface through the
search normalisation below. The search engine applies the same table to
every query, so the two always agree.

| class | rule |
|---|---|
| removed | U+064B to U+065F, U+0670, U+0671 |
| alef | `أ` `إ` `آ` `ٱ` become `ا` |
| ya | `ى` `ئ` `ی` `ے` become `ي` |
| waw | `ؤ` becomes `و` |
| ha and ta marbuta | `ۀ` `ە` become `ه`; `ۃ` becomes `ة` |
| Persian and Urdu letters | `ک` `گ` `ڭ` become `ك`; `ٹ` becomes `ت`; `پ` becomes `ب`; `چ` becomes `ج`; `ژ` becomes `ز`; `ڤ` becomes `ف`; `ڨ` becomes `ق` |

`root_text` leaves out tokens without a root, so its positions do not line
up with the other two fields. For that reason it is indexed without
positions (§7).

## 5. `corpus.db`

Each distinct value is stored once and referred to by number, and each page
is stored as a compressed list of token numbers. The database holds no text
that is not a token.

```
roots(id, root)                     lemmas(id, lemma)         pos_types(id, pos)
feature_sets(id, features JSON)     clitic_sets(id, clitics JSON)
token_definitions(id, surface, lemma_id, root_id, pos_id, feature_set_id, clitic_set_id, triple_id)
triples(id, surface, lemma_id, root_id)         one per distinct (surface, lemma, root)
page_tokens(book_id, part_index, page_id, encoding, token_ids BLOB)
token_codec(key, data, meta)
db_info(corpus_version, schema_version, built_at, book_count, page_count, token_count)
```

A page's blob is the token count followed by a zstd frame, compressed with a
trained dictionary, of each token's frequency rank. The most common words
therefore take one byte each. `token_codec` holds the dictionary and the
table from rank back to token definition.

A triple is the unit that search matches: the normalised surface, lemma and
root of a token, without part of speech, features or clitics. Triples are
numbered in order of frequency.

## 6. `metadata.db`

```
authors(id, author)        genres(id, genre)
books(id, corpus, title, author_id, death_ah, century_ah, genre_id, page_count, token_count,
      parts, original_id, paginated, tags, book_meta, author_meta, metadata_json, citation_json, in_corpus)
db_info(corpus_version, schema_version, built_at, book_count, page_count, token_count)
```

Every row of the master is loaded, including rows with `in_corpus = 0`. A
death date of 0 or below is stored as no date.
`page_count`, `token_count` and `parts` are recounted from `corpus.db` after
each build rather than read from the spreadsheet.

## 7. `tantivy_index/`

The index holds one document per page, in reading order. Books are sorted by
death date and id, and pages by part and page. The index is merged into a
single segment, and `check_order` confirms the order. Each page's `death_ah` is
read from `metadata.db` at this stage, unknown years sort last, and a book with
`in_corpus = 0` is left out even if its JSONL exists.

| field | kind | content |
|---|---|---|
| `text_id`, `part_index`, `page_id` | number, stored | the page's address |
| `death_ah` | number, not stored | the reading-order key; absent when the year is unknown |
| `part_label`, `page_number` | string, stored | for display |
| `body` | stored only | the JSONL body, unchanged |
| `tokens` | text with positions | one term per token, its triple number written as seven digits |
| `root_text` | text without positions | the JSONL `root_text` |

Surface, lemma and root searches are all translated into sets of triple
numbers. One positional field therefore serves all three, and phrase and
proximity searches work across them.

## 8. `boundary_index/`

A second, small index with one document for each pair of consecutive pages
in a book. Each document holds the last 20 tokens of the first page and the
first 20 of the next. The engine keeps only the hits that cross the page
break, so a phrase split between two pages can still be found. It carries the
same `death_ah` as the main index, because the engine merges results from the
two by that key. The build stops if the two indices disagree about any book.

## 9. `toc.db`

Every `<title>` heading in the corpus, with its book, its parent heading and
the page it falls on, for building tables of contents.

## 10. Versions

A corpus version follows semantic versioning from a reader's point of view.
A change that moves a page boundary or renumbers `page_id` is a major
version, because every citation into that book changes with it. Adding books
is a minor version. A change to the metadata alone keeps the version: it
replaces `metadata.db`, and rebuilds the two indices if a death date or
`in_corpus` changed.

## 11. What is not normalised

- `body` has no Unicode normalisation form applied. It keeps diacritics,
  tatweel, hamza forms and presentation-form ligatures.
- Tatweel remains in `surface` and `lemma`, and the search table does not
  remove it.
- `lemma` keeps hamza and alef forms as CAMeL writes them, and only its
  diacritics are removed.
- Presentation forms (U+FB50 to U+FDFF, U+FE70 to U+FEFF) end a word rather
  than belonging to one, so a word written in them produces no token.
- `features` records state, number, gender and case only. Aspect, person,
  voice and mood are not kept.
