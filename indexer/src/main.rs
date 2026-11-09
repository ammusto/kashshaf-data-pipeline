//! Builds the search index from processed JSONL. `death_ah` comes from metadata.db.

use anyhow::{Context, Result};
use indicatif::{ProgressBar, ProgressStyle};
use kashshaf_indexer::{
    load_exclude_set, merge_all_segments, print_size_report, register_tokenizers,
};
use rusqlite::{Connection, OpenFlags};
use serde::Deserialize;
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use tantivy::schema::*;
use tantivy::indexer::NoMergePolicy;
use tantivy::store::{Compressor, ZstdCompressor};
use tantivy::{doc, Index, IndexSettings, IndexWriter};

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct Clitic {
    #[serde(rename = "type")]
    clitic_type: String,
    display: String,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct Token {
    idx: u64,
    surface: String,
    lemma: String,
    root: Option<String>,
    pos: String,
    features: Vec<String>,
    clitics: Vec<Clitic>,
}

#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct PageDocument {
    id: u64,
    part_index: u64,
    page_id: u64,
    part_label: Option<String>,
    page_number: Option<String>,
    body: Option<String>,
    surface_text: String,
    lemma_text: String,
    root_text: String,
    #[serde(default)]
    tokens: Vec<Token>,
}

fn load_books(metadata_db: &Path) -> Result<HashMap<u64, Option<u64>>> {
    let conn = Connection::open_with_flags(metadata_db, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("opening {}", metadata_db.display()))?;
    let mut stmt = conn.prepare("SELECT id, death_ah FROM books WHERE COALESCE(in_corpus, 1) = 1")?;
    let books = stmt
        .query_map([], |r| {
            Ok((r.get::<_, i64>(0)? as u64, r.get::<_, Option<i64>>(1)?.map(|v| v as u64)))
        })?
        .collect::<std::result::Result<_, _>>()?;
    Ok(books)
}

fn build_schema(compound: bool, keep_root_text: bool) -> Schema {
    let mut schema_builder = Schema::builder();

    schema_builder.add_u64_field("text_id", INDEXED | FAST | STORED);
    schema_builder.add_u64_field("page_id", INDEXED | FAST | STORED);
    schema_builder.add_u64_field("part_index", INDEXED | FAST | STORED);

    schema_builder.add_u64_field("death_ah", FAST);

    schema_builder.add_text_field("part_label", STRING | STORED | FAST);
    schema_builder.add_text_field("page_number", STRING | STORED | FAST);

    schema_builder.add_text_field("body", STORED);

    let text_options = TextOptions::default().set_indexing_options(
        TextFieldIndexing::default()
            .set_tokenizer("whitespace")
            .set_index_option(IndexRecordOption::WithFreqsAndPositions),
    );
    if compound {
        schema_builder.add_text_field("tokens", text_options);
        if keep_root_text {
            let basic = TextOptions::default().set_indexing_options(
                TextFieldIndexing::default()
                    .set_tokenizer("whitespace")
                    .set_index_option(IndexRecordOption::Basic),
            );
            schema_builder.add_text_field("root_text", basic);
        }
    } else {
        schema_builder.add_text_field("surface_text", text_options.clone());
        schema_builder.add_text_field("lemma_text", text_options.clone());
        schema_builder.add_text_field("root_text", text_options);
    }

    schema_builder.build()
}

const TRIPLE_TOKEN_WIDTH: usize = 7;

fn triple_tokens(def_ids: &[u32], def_to_triple: &[u32]) -> Result<String> {
    let mut out = String::with_capacity(def_ids.len() * (TRIPLE_TOKEN_WIDTH + 1));
    for (i, &d) in def_ids.iter().enumerate() {
        let t = *def_to_triple
            .get(d as usize)
            .ok_or_else(|| anyhow::anyhow!("definition id {} has no triple", d))?;
        if t == 0 {
            anyhow::bail!("definition id {} maps to triple 0", d);
        }
        if i > 0 {
            out.push(' ');
        }
        out.push_str(&format!("{:0width$}", t, width = TRIPLE_TOKEN_WIDTH));
    }
    Ok(out)
}

fn get_jsonl_files(dir: &Path) -> Result<Vec<PathBuf>> {
    let files: Vec<_> = fs::read_dir(dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().map_or(false, |ext| ext == "jsonl"))
        .collect();
    Ok(files)
}

fn book_id_from_path(path: &Path) -> Option<u64> {
    path.file_stem()?.to_str()?.parse::<u64>().ok()
}

fn sort_reading_order(files: Vec<PathBuf>, books: &HashMap<u64, Option<u64>>) -> Vec<PathBuf> {
    let mut keyed: Vec<(u64, u64, PathBuf)> = files
        .into_iter()
        .map(|path| {
            let id = book_id_from_path(&path).unwrap_or(u64::MAX);
            let death = books.get(&id).copied().flatten().unwrap_or(u64::MAX);
            (death, id, path)
        })
        .collect();
    keyed.sort();
    keyed.into_iter().map(|(_, _, p)| p).collect()
}

fn total_bytes(files: &[PathBuf]) -> u64 {
    files
        .iter()
        .filter_map(|p| fs::metadata(p).ok())
        .map(|m| m.len())
        .sum()
}

#[derive(Debug, Clone)]
struct Options {
    input_dir: PathBuf,
    index_path: PathBuf,
    rebuild: bool,
    exclude_path: Option<PathBuf>,
    threads: usize,
    heap_bytes: usize,
    /// None = LZ4 (tantivy default); Some(level) = zstd at that level.
    zstd_level: Option<i32>,
    blocksize: usize,
    reading_order: bool,
    merge: bool,
    compound: bool,
    corpus_db: Option<PathBuf>,
    keep_root_text: bool,
    metadata_db: Option<PathBuf>,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            input_dir: PathBuf::from("../data/processed"),
            index_path: PathBuf::from("../data/tantivy_index"),
            rebuild: true,
            exclude_path: None,
            threads: 1,
            heap_bytes: 2_000_000_000,
            zstd_level: Some(3),
            blocksize: 65_536,
            reading_order: true,
            merge: true,
            compound: false,
            corpus_db: None,
            keep_root_text: false,
            metadata_db: None,
        }
    }
}

fn index_settings(opts: &Options) -> IndexSettings {
    IndexSettings {
        docstore_compression: match opts.zstd_level {
            Some(level) => Compressor::Zstd(ZstdCompressor {
                compression_level: Some(level),
            }),
            None => Compressor::Lz4,
        },
        docstore_blocksize: opts.blocksize,
        ..Default::default()
    }
}

fn index_pages(opts: &Options, exclude_set: &HashSet<u64>) -> Result<()> {
    let schema = build_schema(opts.compound, opts.keep_root_text);

    let metadata_db = opts
        .metadata_db
        .as_ref()
        .ok_or_else(|| anyhow::anyhow!("--metadata-db PATH is required"))?;
    let books = load_books(metadata_db)?;
    println!("{} in-corpus books in {:?}", books.len(), metadata_db);

    let token_source = if opts.compound {
        let db = opts
            .corpus_db
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("--compound requires --corpus-db PATH"))?;
        let t0 = std::time::Instant::now();
        let src = kashshaf_indexer::blob::TokenSource::open(db)?;
        println!(
            "Compound mode: {} definitions -> triples up to id {} loaded from {:?} in {} ms",
            src.def_to_triple.len(),
            src.max_triple_id,
            db,
            t0.elapsed().as_millis()
        );
        if (src.max_triple_id as u64) >= 10u64.pow(TRIPLE_TOKEN_WIDTH as u32) {
            anyhow::bail!("triple ids exceed {} digits", TRIPLE_TOKEN_WIDTH);
        }
        Some(src)
    } else {
        None
    };

    let index = if opts.rebuild {
        println!("Mode: REBUILD (creating fresh index)");
        if opts.index_path.exists() {
            println!("Removing existing index at {:?}...", opts.index_path);
            fs::remove_dir_all(&opts.index_path)?;
        }
        fs::create_dir_all(&opts.index_path)?;
        Index::builder()
            .schema(schema.clone())
            .settings(index_settings(opts))
            .create_in_dir(&opts.index_path)?
    } else if opts.index_path.exists() {
        println!("Mode: APPEND (adding to existing index; docstore settings come from the index)");
        Index::open_in_dir(&opts.index_path)?
    } else {
        println!("Mode: APPEND (no existing index, creating new)");
        fs::create_dir_all(&opts.index_path)?;
        Index::builder()
            .schema(schema.clone())
            .settings(index_settings(opts))
            .create_in_dir(&opts.index_path)?
    };
    register_tokenizers(&index);
    println!(
        "Docstore: {:?}, blocksize {}",
        index.settings().docstore_compression,
        index.settings().docstore_blocksize
    );

    let text_id_field = schema.get_field("text_id").unwrap();
    let page_id_field = schema.get_field("page_id").unwrap();
    let part_index_field = schema.get_field("part_index").unwrap();
    let death_ah_field = schema.get_field("death_ah").unwrap();
    let part_label_field = schema.get_field("part_label").unwrap();
    let page_number_field = schema.get_field("page_number").unwrap();
    let body_field = schema.get_field("body").unwrap();
    let surface_text_field = schema.get_field("surface_text").ok();
    let lemma_text_field = schema.get_field("lemma_text").ok();
    let root_text_field = schema.get_field("root_text").ok();
    let tokens_field = schema.get_field("tokens").ok();
    let mut alignment_mismatches: u64 = 0;
    let mut missing_blobs: u64 = 0;

    let mut index_writer: IndexWriter =
        index.writer_with_num_threads(opts.threads, opts.heap_bytes)?;
    // No background merges: tantivy's policy would scramble the reading order.
    index_writer.set_merge_policy(Box::new(NoMergePolicy));

    let all_jsonl_files = get_jsonl_files(&opts.input_dir)?;
    println!("Found {} JSONL files in {:?}", all_jsonl_files.len(), opts.input_dir);

    let mut excluded_count = 0usize;
    let mut unparseable_count = 0usize;
    let jsonl_files: Vec<PathBuf> = all_jsonl_files
        .into_iter()
        .filter(|p| match book_id_from_path(p) {
            Some(id) if exclude_set.contains(&id) || !books.contains_key(&id) => {
                excluded_count += 1;
                false
            }
            Some(_) => true,
            None => {
                unparseable_count += 1;
                false
            }
        })
        .collect();

    if excluded_count > 0 {
        println!("Skipping {} books that are not in the corpus", excluded_count);
    }
    if unparseable_count > 0 {
        println!(
            "NOTE: {} JSONL files have non-numeric stems and were skipped",
            unparseable_count
        );
    }

    let jsonl_files = if opts.reading_order {
        sort_reading_order(jsonl_files, &books)
    } else {
        let mut f = jsonl_files;
        f.sort();
        f
    };
    println!("Indexing {} JSONL files", jsonl_files.len());

    if opts.reading_order && opts.threads > 1 {
        println!(
            "WARNING: reading-order ingestion with {} writer threads does not \
             guarantee monotonic doc ids; use --threads 1 for a check_order-clean index.",
            opts.threads
        );
    }

    let total = total_bytes(&jsonl_files);
    let pb = ProgressBar::new(total);
    pb.set_style(
        ProgressStyle::default_bar()
            .template(
                "{spinner:.green} [{elapsed_precise}] [{bar:40.cyan/blue}] {bytes}/{total_bytes} ({eta}) {msg}",
            )
            .unwrap()
            .progress_chars("#>-"),
    );

    let mut indexed_count: u64 = 0;
    let mut error_count: u64 = 0;
    let mut files_done: usize = 0;

    for jsonl_path in &jsonl_files {
        let death_ah = book_id_from_path(jsonl_path)
            .and_then(|id| books.get(&id).copied())
            .with_context(|| format!("no book for {:?}", jsonl_path))?;
        let file =
            File::open(jsonl_path).with_context(|| format!("Failed to open {:?}", jsonl_path))?;
        let reader = BufReader::new(file);

        for (line_num, line_result) in reader.lines().enumerate() {
            let line = match line_result {
                Ok(l) => l,
                Err(e) => {
                    error_count += 1;
                    if error_count <= 5 {
                        eprintln!(
                            "Error reading line {} in {:?}: {}",
                            line_num + 1,
                            jsonl_path,
                            e
                        );
                    }
                    continue;
                }
            };
            pb.inc(line.len() as u64 + 1);

            if line.trim().is_empty() {
                continue;
            }

            match serde_json::from_str::<PageDocument>(&line) {
                Ok(page) => {
                    let mut doc = doc!(
                        text_id_field => page.id,
                        page_id_field => page.page_id,
                        part_index_field => page.part_index,
                    );

                    match &token_source {
                        None => {
                            doc.add_text(surface_text_field.unwrap(), &page.surface_text);
                            doc.add_text(lemma_text_field.unwrap(), &page.lemma_text);
                            doc.add_text(root_text_field.unwrap(), &page.root_text);
                        }
                        Some(src) => {
                            match src.page_ids(page.id, page.part_index, page.page_id)? {
                                Some(ids) => {
                                    let expected = page.surface_text.split_whitespace().count();
                                    if ids.len() != expected {
                                        alignment_mismatches += 1;
                                        if alignment_mismatches <= 5 {
                                            eprintln!(
                                                "ALIGNMENT: book {} part {} page {}: {} token ids vs {} surface tokens",
                                                page.id, page.part_index, page.page_id, ids.len(), expected
                                            );
                                        }
                                    }
                                    doc.add_text(tokens_field.unwrap(), &triple_tokens(&ids, &src.def_to_triple)?);
                                }
                                None => {
                                    missing_blobs += 1;
                                    if missing_blobs <= 5 {
                                        eprintln!(
                                            "MISSING BLOB: book {} part {} page {} not in corpus.db",
                                            page.id, page.part_index, page.page_id
                                        );
                                    }
                                }
                            }
                            if let Some(rt) = root_text_field {
                                doc.add_text(rt, &page.root_text);
                            }
                        }
                    }

                    if let Some(death_ah) = death_ah {
                        doc.add_u64(death_ah_field, death_ah);
                    }

                    if let Some(part_label) = &page.part_label {
                        doc.add_text(part_label_field, part_label);
                    }
                    if let Some(page_number) = &page.page_number {
                        doc.add_text(page_number_field, page_number);
                    }
                    if let Some(body) = &page.body {
                        doc.add_text(body_field, body);
                    }

                    index_writer.add_document(doc)?;
                    indexed_count += 1;
                }
                Err(e) => {
                    error_count += 1;
                    if error_count <= 5 {
                        eprintln!(
                            "Error parsing line {} in {:?}: {}",
                            line_num + 1,
                            jsonl_path,
                            e
                        );
                    }
                }
            }

            if indexed_count % 100_000 == 0 && indexed_count > 0 {
                index_writer.commit()?;
                pb.set_message(format!("committed {} docs", indexed_count));
            }
        }
        files_done += 1;
        if files_done % 250 == 0 {
            pb.set_message(format!("{} files, {} docs", files_done, indexed_count));
        }
    }

    pb.finish_with_message("Indexing complete");

    println!("\nCommitting final documents...");
    index_writer.commit()?;

    let segments_before = if opts.merge {
        merge_all_segments(&index, index_writer)?
    } else {
        index_writer.wait_merging_threads()?;
        index.searchable_segment_ids()?.len()
    };

    println!("\n========================================");
    println!("INDEXING COMPLETE");
    println!("========================================");
    println!("Documents indexed: {}", indexed_count);
    println!("Books excluded:    {}", excluded_count);
    println!("Errors:            {}", error_count);
    if opts.compound {
        println!("Compound mode:     alignment mismatches {}, pages without blob {}", alignment_mismatches, missing_blobs);
    }
    println!(
        "Segments:          {} before merge, {} now",
        segments_before,
        index.searchable_segment_ids()?.len()
    );
    println!("Index location:    {:?}", opts.index_path);
    println!();
    print_size_report(&opts.index_path)?;

    Ok(())
}

fn print_usage() {
    eprintln!("Usage: kashshaf-indexer [--rebuild | --append] [options]");
    eprintln!();
    eprintln!("Options:");
    eprintln!("  --rebuild             Clear existing index and rebuild (default)");
    eprintln!("  --append              Add new documents to existing index");
    eprintln!("  --input DIR           JSONL directory (default ../data/processed)");
    eprintln!("  --index DIR           Index directory (default ../data/tantivy_index)");
    eprintln!("  --exclude PATH        Text file of book IDs to skip (u64 per line, # comments)");
    eprintln!("  --threads N           Writer threads (default 1; >1 breaks reading order)");
    eprintln!("  --heap-gb N           Writer heap in GB (default 2)");
    eprintln!("  --zstd-level N        Docstore zstd level (default 3)");
    eprintln!("  --lz4                 Use LZ4 docstore instead of zstd");
    eprintln!("  --blocksize BYTES     Docstore block size (default 65536)");
    eprintln!("  --no-reading-order    Ingest files in name order instead of (death_ah, text_id)");
    eprintln!("  --no-merge            Leave multiple segments (skip final merge + GC)");
    eprintln!("  --metadata-db PATH    metadata.db: the corpus's books and their death years (required)");
    eprintln!("  --compound            Index one `tokens` field of triple ids read from corpus.db");
    eprintln!("  --corpus-db PATH      corpus.db with triples (required with --compound)");
    eprintln!("  --keep-root-text      With --compound, also index root_text without positions (hedge)");
    eprintln!();
    eprintln!("Examples:");
    eprintln!("  kashshaf-indexer --rebuild --compound --keep-root-text --corpus-db ../data/build/1.0.0/corpus.db --metadata-db ../data/build/1.0.0/metadata.db --input ../data/processed --index ../data/build/1.0.0/tantivy_index");
}

fn parse_args() -> Result<Options> {
    let args: Vec<String> = env::args().collect();
    let mut opts = Options::default();

    let mut i = 1;
    while i < args.len() {
        let need_value = |i: usize, name: &str| -> Result<String> {
            args.get(i + 1)
                .cloned()
                .with_context(|| format!("{} requires a value", name))
        };
        match args[i].as_str() {
            "--rebuild" => opts.rebuild = true,
            "--append" => opts.rebuild = false,
            "--input" => {
                opts.input_dir = PathBuf::from(need_value(i, "--input")?);
                i += 1;
            }
            "--index" => {
                opts.index_path = PathBuf::from(need_value(i, "--index")?);
                i += 1;
            }
            "--exclude" => {
                opts.exclude_path = Some(PathBuf::from(need_value(i, "--exclude")?));
                i += 1;
            }
            "--threads" => {
                opts.threads = need_value(i, "--threads")?.parse()?;
                i += 1;
            }
            "--heap-gb" => {
                let gb: f64 = need_value(i, "--heap-gb")?.parse()?;
                opts.heap_bytes = (gb * 1e9) as usize;
                i += 1;
            }
            "--zstd-level" => {
                opts.zstd_level = Some(need_value(i, "--zstd-level")?.parse()?);
                i += 1;
            }
            "--lz4" => opts.zstd_level = None,
            "--blocksize" => {
                opts.blocksize = need_value(i, "--blocksize")?.parse()?;
                i += 1;
            }
            "--no-reading-order" => opts.reading_order = false,
            "--no-merge" => opts.merge = false,
            "--compound" => opts.compound = true,
            "--corpus-db" => {
                opts.corpus_db = Some(PathBuf::from(need_value(i, "--corpus-db")?));
                i += 1;
            }
            "--keep-root-text" => opts.keep_root_text = true,
            "--metadata-db" => {
                opts.metadata_db = Some(PathBuf::from(need_value(i, "--metadata-db")?));
                i += 1;
            }
            "--help" | "-h" => {
                print_usage();
                std::process::exit(0);
            }
            other => {
                eprintln!("Unknown argument: {}", other);
                print_usage();
                std::process::exit(1);
            }
        }
        i += 1;
    }
    if opts.threads == 0 {
        anyhow::bail!("--threads must be >= 1");
    }
    Ok(opts)
}

fn main() -> Result<()> {
    let opts = parse_args()?;

    let exclude_set = if let Some(p) = &opts.exclude_path {
        if p.exists() {
            let set = load_exclude_set(p)?;
            println!("Loaded {} book IDs from exclude file: {:?}", set.len(), p);
            set
        } else {
            anyhow::bail!("Exclude file does not exist: {:?}", p);
        }
    } else {
        HashSet::new()
    };

    println!("Kashshaf Tantivy Indexer");
    println!("========================");
    println!("Input:    {:?} (JSONL files)", opts.input_dir);
    println!("Output:   {:?}", opts.index_path);
    println!("Mode:     {}", if opts.rebuild { "REBUILD" } else { "APPEND" });
    println!(
        "Threads:  {}  Heap: {:.1} GB",
        opts.threads,
        opts.heap_bytes as f64 / 1e9
    );
    println!(
        "Docstore: {}  Blocksize: {}",
        match opts.zstd_level {
            Some(l) => format!("zstd level {}", l),
            None => "lz4".to_string(),
        },
        opts.blocksize
    );
    println!(
        "Order:    {}",
        if opts.reading_order {
            "reading order (death_ah, text_id)"
        } else {
            "file name"
        }
    );
    println!(
        "Merge:    {}",
        if opts.merge { "yes (single segment)" } else { "no" }
    );
    if opts.compound {
        println!(
            "Schema:   compound `tokens` field{} from {:?}",
            if opts.keep_root_text { " + root_text (no positions)" } else { "" },
            opts.corpus_db
        );
    } else {
        println!("Schema:   surface_text / lemma_text / root_text");
    }
    match &opts.exclude_path {
        Some(p) => println!("Exclude:  {:?}", p),
        None => println!("Exclude:  (none — indexing all JSONL files)"),
    }
    println!();

    if !opts.input_dir.exists() {
        anyhow::bail!("Input directory does not exist: {:?}", opts.input_dir);
    }

    index_pages(&opts, &exclude_set)?;
    Ok(())
}
