//! Assert that `(death_ah ?? MAX, text_id, part_index, page_id)` never decreases.
//! Exit 0 ordered, 1 violation, 2 several segments without --allow-multi-segment.

use anyhow::{Context, Result};
use kashshaf_indexer::register_tokenizers;
use std::env;
use std::path::PathBuf;
use tantivy::Index;

fn print_usage() {
    eprintln!("Usage: check_order --index PATH [--allow-multi-segment]");
}

fn main() -> Result<()> {
    let args: Vec<String> = env::args().collect();
    let mut index_path: Option<PathBuf> = None;
    let mut allow_multi = false;

    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--index" => {
                i += 1;
                index_path = Some(PathBuf::from(
                    args.get(i).context("--index requires a path")?,
                ));
            }
            "--allow-multi-segment" => allow_multi = true,
            "--help" | "-h" => {
                print_usage();
                return Ok(());
            }
            other => {
                eprintln!("Unknown argument: {}", other);
                print_usage();
                std::process::exit(1);
            }
        }
        i += 1;
    }
    let index_path = index_path.context("--index is required")?;

    let index = Index::open_in_dir(&index_path)
        .with_context(|| format!("Failed to open index at {:?}", index_path))?;
    register_tokenizers(&index);
    let reader = index.reader()?;
    let searcher = reader.searcher();
    let segments = searcher.segment_readers();

    println!("Kashshaf Reading-Order Check");
    println!("============================");
    println!("Index:    {:?}", index_path);
    println!("Segments: {}", segments.len());
    println!("Docs:     {}", searcher.num_docs());
    println!();

    if segments.len() > 1 && !allow_multi {
        eprintln!(
            "FAIL: index has {} segments; reading order is only defined for a single \
             segment. Run `merge` first, or pass --allow-multi-segment to check each \
             segment independently.",
            segments.len()
        );
        std::process::exit(2);
    }

    let mut violations = 0u64;
    let mut checked = 0u64;
    let mut deleted = 0u64;
    let mut null_death = 0u64;

    for (seg_ord, seg) in segments.iter().enumerate() {
        let ff = seg.fast_fields();
        let death = ff.u64("death_ah").context("missing fast field death_ah")?;
        let text = ff.u64("text_id").context("missing fast field text_id")?;
        let part = ff.u64("part_index").context("missing fast field part_index")?;
        let page = ff.u64("page_id").context("missing fast field page_id")?;

        let mut prev: Option<(u64, u64, u64, u64)> = None;
        for doc in 0..seg.max_doc() {
            if seg.is_deleted(doc) {
                deleted += 1;
                continue;
            }
            let d = death.first(doc).unwrap_or(u64::MAX);
            if d == u64::MAX {
                null_death += 1;
            }
            let key = (
                d,
                text.first(doc).unwrap_or(0),
                part.first(doc).unwrap_or(0),
                page.first(doc).unwrap_or(0),
            );
            if let Some(p) = prev {
                if key < p {
                    violations += 1;
                    if violations <= 10 {
                        eprintln!(
                            "  violation: segment {} doc {}: {:?} after {:?}",
                            seg_ord, doc, key, p
                        );
                    }
                }
            }
            prev = Some(key);
            checked += 1;
        }
    }

    println!("Checked:    {}", checked);
    println!("Deleted:    {}", deleted);
    println!("NULL death: {}", null_death);
    println!("Violations: {}", violations);

    if violations > 0 {
        eprintln!("\nFAIL: index is not in reading order");
        std::process::exit(1);
    }
    println!("\nOK: doc ids are monotonic in (death_ah, text_id, part_index, page_id)");
    Ok(())
}
