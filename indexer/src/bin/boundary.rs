//! One document per page break, so a phrase across two pages can be found: the left
//! page's last 20 tokens at positions 0-19, the right page's first 20 at 20-39, padded with `_`.

use anyhow::{anyhow, bail, Context, Result};
use indicatif::{ProgressBar, ProgressStyle};
use kashshaf_indexer::blob::{decode_raw, BlobCodec};
use kashshaf_indexer::{index_size_by_extension, merge_all_segments, register_tokenizers};
use rusqlite::{Connection, OpenFlags};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use tantivy::indexer::NoMergePolicy;
use tantivy::postings::Postings;
use tantivy::schema::*;
use tantivy::{doc, DocSet, Index, IndexWriter, TERMINATED};

pub const SIDE: usize = 20;
pub const WIDTH: usize = 2 * SIDE;
pub const PAD: &str = "_";
const TRIPLE_TOKEN_WIDTH: usize = 7;
pub const META_FILE: &str = "boundary.json";
pub const FORMAT: u32 = 1;

fn schema() -> Schema {
    let mut b = Schema::builder();
    b.add_u64_field("text_id", INDEXED | FAST | STORED);
    b.add_u64_field("part_index", INDEXED | FAST | STORED);
    b.add_u64_field("page_id", INDEXED | FAST | STORED);
    b.add_u64_field("part_index_right", INDEXED | FAST | STORED);
    b.add_u64_field("page_id_right", INDEXED | FAST | STORED);
    b.add_u64_field("len_left", FAST | STORED);
    b.add_u64_field("len_right", FAST | STORED);
    // Maps a left-side position back: `page_len_left - SIDE + pos`.
    b.add_u64_field("page_len_left", FAST | STORED);
    b.add_u64_field("death_ah", FAST);
    let positional = TextOptions::default().set_indexing_options(
        TextFieldIndexing::default()
            .set_tokenizer("whitespace")
            .set_index_option(IndexRecordOption::WithFreqsAndPositions),
    );
    b.add_text_field("tokens", positional);
    let basic = TextOptions::default().set_indexing_options(
        TextFieldIndexing::default()
            .set_tokenizer("whitespace")
            .set_index_option(IndexRecordOption::Basic),
    );
    b.add_text_field("root_text", basic);
    b.build()
}

fn triple_term(t: u32) -> String {
    format!("{:0width$}", t, width = TRIPLE_TOKEN_WIDTH)
}

struct Book {
    id: u64,
    death_ah: Option<u64>,
}

fn books_in_reading_order(metadata_db: &Path) -> Result<Vec<Book>> {
    let conn = Connection::open_with_flags(metadata_db, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("opening {}", metadata_db.display()))?;
    let mut stmt = conn.prepare("SELECT id, death_ah FROM books WHERE COALESCE(in_corpus, 1) = 1")?;
    let mut books: Vec<Book> = stmt
        .query_map([], |r| {
            Ok(Book {
                id: r.get::<_, i64>(0)? as u64,
                death_ah: r.get::<_, Option<i64>>(1)?.map(|v| v as u64),
            })
        })?
        .collect::<std::result::Result<_, _>>()?;
    books.sort_by_key(|b| (b.death_ah.unwrap_or(u64::MAX), b.id));
    Ok(books)
}

struct Corpus {
    conn: Connection,
    codec: Option<BlobCodec>,
    has_encoding: bool,
    def_to_triple: Vec<u32>,
    triple_root: Vec<u32>,
    roots: Vec<String>,
    corpus_version: String,
    schema_version: i64,
}

impl Corpus {
    fn open(path: &Path) -> Result<Self> {
        let conn = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .with_context(|| format!("opening {}", path.display()))?;
        let codec = BlobCodec::load(&conn)?;
        let has_encoding = {
            let mut stmt = conn.prepare("PRAGMA table_info(page_tokens)")?;
            let names: Vec<String> = stmt.query_map([], |r| r.get::<_, String>(1))?.filter_map(|r| r.ok()).collect();
            names.iter().any(|n| n == "encoding")
        };
        let (corpus_version, schema_version): (String, i64) =
            conn.query_row("SELECT corpus_version, schema_version FROM db_info LIMIT 1", [], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let max_def: i64 = conn.query_row("SELECT COALESCE(MAX(id),0) FROM token_definitions", [], |r| r.get(0))?;
        let mut def_to_triple = vec![0u32; max_def as usize + 1];
        {
            let mut stmt = conn.prepare("SELECT id, triple_id FROM token_definitions")?;
            for row in stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?)))? {
                let (id, t) = row?;
                def_to_triple[id as usize] = t.ok_or_else(|| anyhow!("token_definitions.id {} has NULL triple_id", id))? as u32;
            }
        }
        let max_root: i64 = conn.query_row("SELECT COALESCE(MAX(id),0) FROM roots", [], |r| r.get(0))?;
        let mut roots = vec![String::new(); max_root as usize + 1];
        {
            let mut stmt = conn.prepare("SELECT id, root FROM roots")?;
            for row in stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))? {
                let (id, root) = row?;
                roots[id as usize] = root;
            }
        }
        let max_triple: i64 = conn.query_row("SELECT COALESCE(MAX(id),0) FROM triples", [], |r| r.get(0))?;
        let mut triple_root = vec![0u32; max_triple as usize + 1];
        {
            let mut stmt = conn.prepare("SELECT id, root_id FROM triples")?;
            for row in stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?)))? {
                let (id, root) = row?;
                triple_root[id as usize] = root.unwrap_or(0) as u32;
            }
        }
        if (max_triple as u64) >= 10u64.pow(TRIPLE_TOKEN_WIDTH as u32) {
            bail!("triple ids exceed {} digits", TRIPLE_TOKEN_WIDTH);
        }
        Ok(Self { conn, codec, has_encoding, def_to_triple, triple_root, roots, corpus_version, schema_version })
    }

    fn decode(&self, encoding: i64, blob: &[u8]) -> Result<Vec<u32>> {
        match (&self.codec, encoding) {
            (_, 1) => decode_raw(blob),
            (Some(c), 2) => c.decode(2, blob),
            (None, 2) => bail!("encoding-2 blob but corpus.db has no token_codec"),
            (_, e) => bail!("unknown encoding {}", e),
        }
    }

    fn pages(&self, book_id: u64) -> Result<Vec<(u64, u64, Vec<u32>)>> {
        let sql = if self.has_encoding {
            "SELECT part_index, page_id, encoding, token_ids FROM page_tokens WHERE book_id = ?1 ORDER BY part_index, page_id"
        } else {
            "SELECT part_index, page_id, 1, token_ids FROM page_tokens WHERE book_id = ?1 ORDER BY part_index, page_id"
        };
        let mut stmt = self.conn.prepare_cached(sql)?;
        let rows = stmt.query_map([book_id as i64], |r| {
            Ok((r.get::<_, i64>(0)? as u64, r.get::<_, i64>(1)? as u64, r.get::<_, i64>(2)?, r.get::<_, Vec<u8>>(3)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (part, page, enc, blob) = row?;
            let defs = self.decode(enc, &blob)?;
            let mut triples = Vec::with_capacity(defs.len());
            for d in defs {
                let t = *self.def_to_triple.get(d as usize).ok_or_else(|| anyhow!("definition id {} has no triple", d))?;
                if t == 0 {
                    bail!("definition id {} maps to triple 0", d);
                }
                triples.push(t);
            }
            out.push((part, page, triples));
        }
        Ok(out)
    }

    fn page_count(&self) -> Result<u64> {
        Ok(self.conn.query_row("SELECT COUNT(*) FROM page_tokens", [], |r| r.get::<_, i64>(0))? as u64)
    }

    fn books_with_pages(&self) -> Result<u64> {
        Ok(self.conn.query_row("SELECT COUNT(DISTINCT book_id) FROM page_tokens", [], |r| r.get::<_, i64>(0))? as u64)
    }

    fn page(&self, book_id: u64, part_index: u64, page_id: u64) -> Result<Option<Vec<u32>>> {
        let sql = if self.has_encoding {
            "SELECT encoding, token_ids FROM page_tokens WHERE book_id=?1 AND part_index=?2 AND page_id=?3"
        } else {
            "SELECT 1, token_ids FROM page_tokens WHERE book_id=?1 AND part_index=?2 AND page_id=?3"
        };
        let row: Option<(i64, Vec<u8>)> = self
            .conn
            .query_row(sql, rusqlite::params![book_id as i64, part_index as i64, page_id as i64], |r| Ok((r.get(0)?, r.get(1)?)))
            .ok();
        match row {
            None => Ok(None),
            Some((enc, blob)) => Ok(Some(self.decode(enc, &blob)?.into_iter().map(|d| self.def_to_triple[d as usize]).collect())),
        }
    }
}

fn window_text(corpus: &Corpus, left: &[u32], right: &[u32]) -> (String, String, usize, usize) {
    let l = &left[left.len().saturating_sub(SIDE)..];
    let r = &right[..right.len().min(SIDE)];
    let mut tokens = String::with_capacity(WIDTH * (TRIPLE_TOKEN_WIDTH + 1));
    let mut roots = String::new();
    let mut push = |s: &str| {
        if !tokens.is_empty() {
            tokens.push(' ');
        }
        tokens.push_str(s);
    };
    for _ in l.len()..SIDE {
        push(PAD);
    }
    for &t in l.iter().chain(r.iter()) {
        push(&triple_term(t));
        let root = corpus.triple_root.get(t as usize).copied().unwrap_or(0);
        if root != 0 {
            if !roots.is_empty() {
                roots.push(' ');
            }
            roots.push_str(&corpus.roots[root as usize]);
        }
    }
    for _ in r.len()..SIDE {
        push(PAD);
    }
    (tokens, roots, l.len(), r.len())
}

struct BuildOptions {
    corpus_db: PathBuf,
    metadata_db: PathBuf,
    out: PathBuf,
    corpus_version: Option<String>,
    heap_bytes: usize,
}

fn build(o: &BuildOptions) -> Result<()> {
    let t0 = std::time::Instant::now();
    let corpus = Corpus::open(&o.corpus_db)?;
    let version = o.corpus_version.clone().unwrap_or_else(|| corpus.corpus_version.clone());
    let books = books_in_reading_order(&o.metadata_db)?;
    println!(
        "corpus {} (schema {}): {} pages, {} books in metadata; roots {}, triples {}",
        corpus.corpus_version,
        corpus.schema_version,
        corpus.page_count()?,
        books.len(),
        corpus.roots.len(),
        corpus.triple_root.len()
    );

    if o.out.exists() {
        std::fs::remove_dir_all(&o.out)?;
    }
    std::fs::create_dir_all(&o.out)?;
    let schema = schema();
    let index = Index::builder().schema(schema.clone()).create_in_dir(&o.out)?;
    register_tokenizers(&index);
    let f = |n: &str| schema.get_field(n).unwrap();
    let (f_text, f_part, f_page, f_part_r, f_page_r, f_len_l, f_len_r, f_page_len_l) =
        (f("text_id"), f("part_index"), f("page_id"), f("part_index_right"), f("page_id_right"), f("len_left"), f("len_right"), f("page_len_left"));
    let (f_death, f_tokens, f_roots) = (f("death_ah"), f("tokens"), f("root_text"));

    let mut writer: IndexWriter = index.writer_with_num_threads(1, o.heap_bytes)?;
    writer.set_merge_policy(Box::new(NoMergePolicy));

    let pb = ProgressBar::new(books.len() as u64);
    pb.set_style(ProgressStyle::default_bar().template("{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {pos}/{len} books ({eta}) {msg}").unwrap());
    let mut docs: u64 = 0;
    let mut pages: u64 = 0;
    let mut books_with_pages: u64 = 0;
    let mut short_sides: u64 = 0;
    for book in &books {
        let book_pages = corpus.pages(book.id)?;
        if book_pages.is_empty() {
            pb.inc(1);
            continue;
        }
        books_with_pages += 1;
        pages += book_pages.len() as u64;
        for pair in book_pages.windows(2) {
            let (lp, lg, left) = &pair[0];
            let (rp, rg, right) = &pair[1];
            let (tokens, roots, len_l, len_r) = window_text(&corpus, left, right);
            if len_l < SIDE || len_r < SIDE {
                short_sides += 1;
            }
            let mut d = doc!(
                f_text => book.id,
                f_part => *lp,
                f_page => *lg,
                f_part_r => *rp,
                f_page_r => *rg,
                f_len_l => len_l as u64,
                f_len_r => len_r as u64,
                f_page_len_l => left.len() as u64,
                f_tokens => tokens,
            );
            if !roots.is_empty() {
                d.add_text(f_roots, &roots);
            }
            if let Some(v) = book.death_ah {
                d.add_u64(f_death, v);
            }
            writer.add_document(d)?;
            docs += 1;
            if docs % 250_000 == 0 {
                writer.commit()?;
                pb.set_message(format!("{} boundaries", docs));
            }
        }
        pb.inc(1);
    }
    pb.finish_with_message(format!("{} boundaries", docs));
    writer.commit()?;
    let before = merge_all_segments(&index, writer)?;
    let segments = index.searchable_segment_ids()?.len();
    let elapsed = t0.elapsed();

    let (by_ext, total) = index_size_by_extension(&o.out)?;
    let meta = serde_json::json!({
        "format": FORMAT,
        "corpus_version": version,
        "schema_version": corpus.schema_version,
        "side": SIDE,
        "width": WIDTH,
        "midpoint": SIDE,
        "pad": PAD,
        "documents": docs,
        "pages": pages,
        "books": books_with_pages,
        "built_at": chrono_now(),
        "build_seconds": elapsed.as_secs(),
    });
    std::fs::write(o.out.join(META_FILE), serde_json::to_string_pretty(&meta)?)?;

    println!("\nBOUNDARY INDEX COMPLETE");
    println!("  documents:      {} (= {} pages - {} books)", docs, pages, books_with_pages);
    println!("  short sides:    {} documents padded on at least one side", short_sides);
    println!("  segments:       {} before merge, {} now", before, segments);
    println!("  build time:     {:.0} s", elapsed.as_secs_f64());
    println!("  size by extension:");
    for (ext, bytes) in &by_ext {
        println!("    {:12} {:>10.3} GB", ext, *bytes as f64 / 1e9);
    }
    println!("    {:12} {:>10.3} GB", "TOTAL", total as f64 / 1e9);
    if docs != pages - books_with_pages {
        bail!("document count {} != pages {} - books {}", docs, pages, books_with_pages);
    }
    if docs > 0 && segments != 1 {
        bail!("{} segments after merge", segments);
    }
    Ok(())
}

fn chrono_now() -> String {
    let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    format!("{}", secs)
}

fn positions_in_doc(inverted: &tantivy::InvertedIndexReader, term: &Term, doc: u32) -> Vec<u32> {
    let Ok(Some(mut postings)) = inverted.read_postings(term, IndexRecordOption::WithFreqsAndPositions) else {
        return Vec::new();
    };
    if postings.doc() == TERMINATED || postings.doc() > doc {
        return Vec::new();
    }
    let found = if postings.doc() == doc { doc } else { postings.seek(doc) };
    if found != doc {
        return Vec::new();
    }
    let mut out = Vec::new();
    postings.positions(&mut out);
    out
}

fn death_years(searcher: &tantivy::Searcher, name: &str, failures: &mut Vec<String>) -> Result<HashMap<u64, Option<u64>>> {
    let mut years: HashMap<u64, Option<u64>> = HashMap::new();
    for seg in searcher.segment_readers() {
        let ff = seg.fast_fields();
        let text = ff.u64("text_id")?;
        let death = ff.u64("death_ah")?;
        for d in 0..seg.max_doc() {
            let (book, year) = (text.first(d).unwrap_or(0), death.first(d));
            if let Some(prev) = years.insert(book, year) {
                if prev != year {
                    failures.push(format!("{}: book {} has death years {:?} and {:?}", name, book, prev, year));
                }
            }
        }
    }
    Ok(years)
}

/// The engine merges the two indexes by death year and never compares them.
fn check_same_years(boundary: &tantivy::Searcher, main_path: &Path, failures: &mut Vec<String>) -> Result<()> {
    let main_index = Index::open_in_dir(main_path).with_context(|| format!("opening {}", main_path.display()))?;
    let main = main_index.reader()?.searcher();
    let main_years = death_years(&main, "main index", failures)?;
    let years = death_years(boundary, "boundary index", failures)?;
    for (book, year) in &years {
        match main_years.get(book) {
            None => failures.push(format!("book {} is in the boundary index but not the main index", book)),
            Some(m) if m != year => failures.push(format!("book {}: death year {:?} in the main index, {:?} in the boundary index", book, m, year)),
            Some(_) => {}
        }
    }
    println!("death years compared for {} books against the main index", years.len());
    Ok(())
}

fn check(index_path: &Path, corpus_db: &Path, main_index: &Path, sample: usize) -> Result<()> {
    let meta_path = index_path.join(META_FILE);
    let meta: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&meta_path).with_context(|| format!("reading {}", meta_path.display()))?)?;
    let corpus = Corpus::open(corpus_db)?;
    let mut failures: Vec<String> = Vec::new();

    if meta["format"].as_u64() != Some(FORMAT as u64) {
        failures.push(format!("format {} (this build reads {})", meta["format"], FORMAT));
    }
    if meta["corpus_version"].as_str() != Some(corpus.corpus_version.as_str()) {
        failures.push(format!("stamped for corpus {} but corpus.db is {}", meta["corpus_version"], corpus.corpus_version));
    }
    if meta["midpoint"].as_u64() != Some(SIDE as u64) || meta["width"].as_u64() != Some(WIDTH as u64) {
        failures.push(format!("width/midpoint {}/{} (expected {}/{})", meta["width"], meta["midpoint"], WIDTH, SIDE));
    }

    let index = Index::open_in_dir(index_path)?;
    register_tokenizers(&index);
    let reader = index.reader()?;
    let searcher = reader.searcher();
    let docs = searcher.num_docs();
    let expected = corpus.page_count()? - corpus.books_with_pages()?;
    println!("boundary_index: {} documents, corpus {}, {} segment(s)", docs, meta["corpus_version"], searcher.segment_readers().len());
    if docs != expected {
        failures.push(format!("{} documents, expected pages - books = {}", docs, expected));
    }
    if docs > 0 && searcher.segment_readers().len() != 1 {
        failures.push(format!("{} segments; must be one", searcher.segment_readers().len()));
    }
    if meta["documents"].as_u64() != Some(docs) {
        failures.push(format!("{} says {} documents, the index has {}", META_FILE, meta["documents"], docs));
    }

    check_same_years(&searcher, main_index, &mut failures)?;

    let schema = index.schema();
    let f_tokens = schema.get_field("tokens")?;
    let mut checked = 0usize;
    let mut last_key: Option<(u64, u64, u64, u64)> = None;
    for seg in searcher.segment_readers() {
        let ff = seg.fast_fields();
        let text = ff.u64("text_id")?;
        let part = ff.u64("part_index")?;
        let page = ff.u64("page_id")?;
        let part_r = ff.u64("part_index_right")?;
        let page_r = ff.u64("page_id_right")?;
        let len_l = ff.u64("len_left")?;
        let len_r = ff.u64("len_right")?;
        let page_len_l = ff.u64("page_len_left")?;
        let death = ff.u64("death_ah")?;
        let inverted = seg.inverted_index(f_tokens)?;
        let n = seg.max_doc();
        let step = (n as usize / sample.max(1)).max(1) as u32;
        let mut d = 0u32;
        while d < n {
            let (b, lp, lg, rp, rg) = (
                text.first(d).unwrap_or(0),
                part.first(d).unwrap_or(0),
                page.first(d).unwrap_or(0),
                part_r.first(d).unwrap_or(0),
                page_r.first(d).unwrap_or(0),
            );
            let key = (death.first(d).unwrap_or(u64::MAX), b, lp, lg);
            if let Some(prev) = last_key {
                if key < prev {
                    failures.push(format!("doc {} is out of reading order ({:?} after {:?})", d, key, prev));
                }
            }
            last_key = Some(key);
            let (ll, lr) = (len_l.first(d).unwrap_or(0) as usize, len_r.first(d).unwrap_or(0) as usize);
            let left = corpus.page(b, lp, lg)?.ok_or_else(|| anyhow!("doc {}: left page {}:{}:{} missing from corpus.db", d, b, lp, lg))?;
            let right = corpus.page(b, rp, rg)?.ok_or_else(|| anyhow!("doc {}: right page {}:{}:{} missing from corpus.db", d, b, rp, rg))?;
            if ll != left.len().min(SIDE) || lr != right.len().min(SIDE) {
                failures.push(format!("doc {} ({}:{}:{}): len_left {} len_right {} but pages have {} and {} tokens", d, b, lp, lg, ll, lr, left.len(), right.len()));
            }
            let pl = page_len_l.first(d).unwrap_or(0) as usize;
            if pl != left.len() {
                failures.push(format!("doc {} ({}:{}:{}): page_len_left {} but the page has {} tokens", d, b, lp, lg, pl, left.len()));
            }
            if let Some(&t) = right.first() {
                let p = positions_in_doc(&inverted, &Term::from_field_text(f_tokens, &triple_term(t)), d);
                if !p.contains(&(SIDE as u32)) {
                    failures.push(format!("doc {} ({}:{}:{}): first right token {} not at position {} (positions {:?})", d, b, lp, lg, t, SIDE, p));
                }
            }
            if let Some(&t) = left.last() {
                let p = positions_in_doc(&inverted, &Term::from_field_text(f_tokens, &triple_term(t)), d);
                if !p.contains(&(SIDE as u32 - 1)) {
                    failures.push(format!("doc {} ({}:{}:{}): last left token {} not at position {} (positions {:?})", d, b, lp, lg, t, SIDE - 1, p));
                }
            }
            checked += 1;
            if failures.len() > 20 {
                break;
            }
            d = d.saturating_add(step);
        }
    }
    println!("midpoint invariant checked on {} documents", checked);
    let (by_ext, total) = index_size_by_extension(index_path)?;
    for (ext, bytes) in &by_ext {
        println!("  {:12} {:>10.3} GB", ext, *bytes as f64 / 1e9);
    }
    println!("  {:12} {:>10.3} GB", "TOTAL", total as f64 / 1e9);
    if !failures.is_empty() {
        for f in &failures {
            println!("FAIL {}", f);
        }
        bail!("{} check failure(s)", failures.len());
    }
    println!("boundary_index: ok");
    Ok(())
}

fn usage() {
    eprintln!("Usage:");
    eprintln!("  kashshaf-boundary --corpus-db PATH --metadata-db PATH --out DIR [--corpus-version V] [--heap BYTES]");
    eprintln!("  kashshaf-boundary --check --index DIR --corpus-db PATH --main-index DIR [--sample N]");
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let mut opts: HashMap<String, String> = HashMap::new();
    let mut i = 1;
    let mut check_mode = false;
    while i < args.len() {
        match args[i].as_str() {
            "--check" => check_mode = true,
            "-h" | "--help" => {
                usage();
                return Ok(());
            }
            flag if flag.starts_with("--") => {
                i += 1;
                let v = args.get(i).ok_or_else(|| anyhow!("{} requires a value", flag))?;
                opts.insert(flag.to_string(), v.clone());
            }
            other => bail!("unexpected argument {}", other),
        }
        i += 1;
    }
    let get = |k: &str| opts.get(k).map(PathBuf::from);
    if check_mode {
        let index = get("--index").ok_or_else(|| anyhow!("--check needs --index"))?;
        let corpus_db = get("--corpus-db").ok_or_else(|| anyhow!("--check needs --corpus-db"))?;
        let main_index = get("--main-index").ok_or_else(|| anyhow!("--check needs --main-index"))?;
        let sample: usize = opts.get("--sample").map(|s| s.parse()).transpose()?.unwrap_or(2000);
        return check(&index, &corpus_db, &main_index, sample);
    }
    let o = BuildOptions {
        corpus_db: get("--corpus-db").ok_or_else(|| anyhow!("--corpus-db is required"))?,
        metadata_db: get("--metadata-db").ok_or_else(|| anyhow!("--metadata-db is required"))?,
        out: get("--out").ok_or_else(|| anyhow!("--out is required"))?,
        corpus_version: opts.get("--corpus-version").cloned(),
        heap_bytes: opts.get("--heap").map(|s| s.parse()).transpose()?.unwrap_or(1_000_000_000),
    };
    build(&o)
}
