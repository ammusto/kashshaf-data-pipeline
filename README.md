# kashshaf-data-pipeline

This is the pipeline that builds the text corpus for
[Kashshaf](https://kashshaf.com). It takes Arabic texts from four
sources, cleans them, analyses each token with
[CAMeL Tools](https://github.com/CAMeL-Lab/camel_tools), and builds the
SQLite databases and Tantivy search indices that the app reads.

It is published so that others can see exactly how the texts are cleaned and
processed, and can run the same steps on texts of their own. You can use any
mix of the four sources, from a handful of books to tens of thousands.
Publishing and release tooling are not part of this repository.

Every cleaning and normalisation step, and every file format, is described
in [docs/CORPUS_DATA_MODEL.md](docs/CORPUS_DATA_MODEL.md).

## The five stages

| stage | what it does | run with |
|---|---|---|
| 1. Extract | gets texts from their sources into `data/raw/` | `extract/`, or files you already have |
| 2. Metadata | reads each source's metadata and writes `metadata/metadata.xlsx`, which you can use as it is or edit | `scripts/collect_metadata.py` |
| 3. Convert | turns every raw text into one canonical JSON format and cleans it | `scripts/kashshaf_pipeline.py` |
| 4. Morphology | tokenises each page and tags every word with lemma, root and part of speech | `scripts/process_batch.py` |
| 5. Build | writes the databases and the search indices | `scripts/build_sqlite_tokens.py` and the Rust indexer |

Stages 3 to 5 run together with one command, `scripts/build_corpus.py`.

## Folder layout

```
kashshaf-data-pipeline/
  data/
    raw/
      openiti/      OpenITI mARkdown files, one per version URI
      shamela/      Shamela JSON, one per book
      ahlbayt/      Ahl al-Bayt library JSON, one per work
      nusus/        nusus TEI, one <text_id>.xml per text, and text_contrib.csv
    canonical/      stage 3 output
    processed/      stage 4 output, one JSONL file per book
    build/          stage 5 output, one folder per corpus version
  metadata/
    sources/        stage 2 output, one table per source
    metadata.xlsx   the master metadata, written in stage 2 and edited by you
  extract/          tools that fetch or extract raw texts
  scripts/          the pipeline itself, stages 2 to 5
  indexer/          the Rust programs that build the search indices
  tests/            unit tests, and a sample corpus in tests/sample/
  docs/             the data model
  config.example.toml
                    a template for config.toml, your machine's paths
  morph_overrides.json, morph_overrides_auto.json
                    corrections applied where the tagger gives up on a word
```

The folders under `data/` start empty. Put your texts into the raw folders
for whichever sources you use and leave the others empty. Nothing under
`data/` or `metadata/sources/` is tracked by git.

## Setup

You will need:

- **Python 3.11.**
- **An NVIDIA GPU with CUDA.** Morphology runs a BERT model. Without a GPU it
  runs on the CPU, which is fine for a few books and very slow for many.
- **Rust**, through [rustup](https://rustup.rs), to compile the indexer.
- **A Java JDK, version 21 or later,** only if you extract texts from Shamela.
- **Adequate disk space (~300GB).** CAMeL Tools will take at least 1 GB, raw files can take up to 30-40GB, processed up to 220GB, and final indices and databases around 10 GB.

```
python -m venv venv
venv\Scripts\activate                     (on Linux or macOS: source venv/bin/activate)
pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cu121
pip install -r requirements.txt
camel_data -i morphology-db-msa-r13
camel_data -i disambig-bert-unfactored-msa
cd indexer && cargo build --release && cd ..
python tests/run_sample.py
```

`run_sample.py` runs every stage on a few pages from each source, in a
temporary folder. If it ends with `ok`, your setup works.

The commands below are written for Windows. On Linux or macOS, use forward
slashes and the same arguments.

### Configuration

Paths that differ between machines live in `config.toml`. Copy
`config.example.toml` to that name and fill in what you use. Every key is
optional, and a command-line argument always overrides it.

| key | meaning | default |
|---|---|---|
| `paths.data` | where raw texts and build output live | `data` |
| `paths.metadata` | where `metadata.xlsx` and `sources/` live | `metadata` |
| `sources.shamela` | your Shamela folder | none |
| `sources.ahlbayt` | the Ahl al-Bayt app's `backend` folder | none |

## Stage 1: Extract texts

Each source arrives differently. You only need the sources you want.

**OpenITI.** `extract/fetch_openiti.py` downloads texts by version URI from
OpenITI's GitHub repositories. You can also copy files from an OpenITI
release on [Zenodo](https://zenodo.org/records/17767721).

```
python extract\fetch_openiti.py --uris 0630IbnCunayn.Diwan.JK007597-ara1
python extract\fetch_openiti.py --uri-file my_texts.txt
```

**Shamela.** This needs an installed copy of the Shamela library (version 4)
and a Java JDK, version 21 or later. Set `sources.shamela` in `config.toml`, then:

```
python extract\shamela\extract_indices.py
python extract\shamela\build_jsons.py
```

The first command exports Shamela's search indices to `data/shamela_export/`,
about 17 GB for the whole library. The second joins them with its databases
and writes one JSON per book to `data/raw/shamela/`. Both accept
`--test-single-book`. The exporter reads Shamela's indices with the Lucene jars
that the install ships in `app/lucene/`, so it follows Shamela's own updates. An
install without them is read with the Lucene 10.4 jars in `extract/shamela/lib/`
(Apache License 2.0).

**Ahl al-Bayt library.** This needs the Maktabat Ahl al-Bayt desktop app,
with its books downloaded, and `sources.ahlbayt` set in `config.toml`. The
extractor reads the app's own databases. It writes one JSON per work to `data/raw/ahlbayt/`, and the library's catalogue
record for each work to `metadata/sources/ahlbayt.xlsx`. By default it takes
works in Arabic by authors who died by 1348 AH.

```
python extract\ab_extractor.py
python extract\ab_extractor.py --dry-run          (selection and metadata only)
```

**Nusus.** Copy the TEI files and `text_contrib.csv` from the
[nusus](https://github.com/ammusto/nusus) repository into `data/raw/nusus/`.

## Stage 2: Metadata

```
python scripts\collect_metadata.py
```

This writes one table per source to `metadata/sources/`, using what that
source says about each book. The OpenITI table comes from OpenITI's release
metadata, which is downloaded from Zenodo the first time. These tables are
yours to read and reuse as they are.

It then writes `metadata/metadata.xlsx`, the master that the build reads, with
one row per book. A new book gets the next free `id`, `in_corpus = 1`, and its
title, author and death date from the source. Books by an author the master
already knows receive that author's `author_id`. Rows that already exist are
never changed, so you can run this again after adding texts.

You can build from this file straight away. These edits are optional, and can
be made before or after a build:

1. **Remove duplicates.** If the same work arrives from two sources, set
   `in_corpus` to `0` on the copy you do not want. The row stays and the
   book is skipped. Nothing removes duplicates for you.
2. **Merge authors.** An author who arrives from two sources under different
   names gets two `author_id`s. Give those rows the same `author_id` and
   `author_name`.
3. **Add genres.** Fill in `genre_id` and `genre` to classify the books in
   `metadata.db`. No source supplies a usable genre list, so they start blank. Each
   `genre_id` must always carry the same `genre` name.
4. **Correct what you like,** such as titles, author names and death dates.
   Each `author_id` must always carry the same `author_name`.

Treat `id` as permanent once a corpus is in use, since citations and links
depend on it. The full list of columns is in section 2.2 of the
[data model](docs/CORPUS_DATA_MODEL.md).

If you already have a master from an earlier build, put it in `metadata/`
before running this stage. Only books it does not list will be added.

## Stage 3: Convert to canonical JSON

```
python scripts\kashshaf_pipeline.py
```

Every raw file whose row has `in_corpus = 1` becomes
`data/canonical/<corpus>.<original_id>.json`, with its pages and headings in
one shared format. OpenITI's mARkdown is split into pages and its headings
are converted. Nusus TEI is read page by page. Shamela and Ahl al-Bayt texts
already have pages. Every page then goes through the same cleaner, which
removes markup, editorial markers, Latin text and stray symbols, and keeps
the Arabic exactly as written. The cleaner's steps are listed in section 3.4
of the data model. This stage takes minutes.

## Stage 4: Morphology

```
python scripts\process_batch.py --all --input-dir data\canonical --output-dir data\processed
```

Each page is tokenised, and every word is tagged by CAMeL Tools' BERT model
for Modern Standard Arabic with its lemma, root, part of speech, case, number,
gender and clitics. Where the tagger cannot identify a word, the override
files supply an analysis. The result is one JSONL file per book, named by its
`id`, with one line per page.

This is the slow stage. On an RTX 4070 Ti it handles about 16,000 tokens a
second, which is roughly 20 hours for the entire Kashshāf corpus (all texts from 1348/1930 and earlier). It records its
progress in `data/processing_progress.json`, so an interrupted run continues from
where it stopped.

## Stage 5: Build databases and indices

The build command runs every step of stages 3 to 5 in order:

```
python scripts\build_corpus.py 1.0.0
```

It starts by checking `metadata.xlsx`, and stops with a list of the rows to
fix if anything the build needs is missing. When it finishes,
`data/build/1.0.0/` contains:

| file | content |
|---|---|
| `corpus.db` | every page as a compressed list of tokens, and every distinct token analysis |
| `metadata.db` | books, authors and genres, from `metadata.xlsx` |
| `tantivy_index/` | the search index, one document per page |
| `boundary_index/` | a small index for phrases that run across a page break |
| `toc.db` | every heading, for tables of contents |

Each step records itself in `data/build/<version>/.build_state.json`, so a
build that stops partway resumes from the step that failed.

```
python scripts\build_corpus.py 1.0.0 --list             (show the steps and what is done)
python scripts\build_corpus.py 1.0.0 --from indexer     (start at a later step)
python scripts\build_corpus.py 1.0.0 --books 812 813    (process only these books and add them to the build)
python scripts\build_corpus.py 1.0.0 --only metadata    (reload metadata.db after editing the spreadsheet)
```

Editing the metadata never requires morphology to run again. Reloading
`metadata.db` is enough for titles, authors, genres and centuries. If you
changed a death date, which sets the order of search results, or `in_corpus`,
also rebuild the indices:

```
python scripts\build_corpus.py 1.0.0 --from indexer --force
```

`--books` adds to a build that already exists in that version's folder. To
release the result as a new version, copy the previous version's folder to
the new name first and run `--books` there.

A change that moves page boundaries, such as re-downloading an OpenITI text,
changes the page numbers that citations rely on. Give that build a new major
version. Adding books is a minor version.

## Tests

```
python -m pytest tests -q
```

The tests cover the cleaner, the Ahl al-Bayt extractor, the nusus converter
and the metadata checks. With the nusus TEI in place, the converter is also
checked against the 69 nusus texts as they appear in Kashshaf.

`tests/run_sample.py` runs the pipeline itself on `tests/sample/`, which has
the same folders as `data/` and `metadata/` and a short excerpt from each
source:

```
python tests\run_sample.py                             (all four sources)
python tests\run_sample.py --corpora openiti nusus     (only these)
python tests\run_sample.py --extract                   (also try the extractors)
```

With `--extract`, each extractor runs on one text using the paths in
`config.toml`. A source that is not available on your machine is reported as
skipped, and does not fail the run.

## License

MIT. See [LICENSE](LICENSE).
