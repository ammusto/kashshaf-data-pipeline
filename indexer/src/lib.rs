
pub mod blob;

use anyhow::{Context, Result};
use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File};
use std::io::Read;
use std::path::Path;
use tantivy::{Index, IndexWriter};

pub fn load_exclude_set(path: &Path) -> Result<HashSet<u64>> {
    let mut content = String::new();
    File::open(path)
        .with_context(|| format!("Failed to open exclude file {:?}", path))?
        .read_to_string(&mut content)?;
    let mut set = HashSet::new();
    for (lineno, line) in content.lines().enumerate() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let id: u64 = trimmed.parse().with_context(|| {
            format!(
                "Bad book id at {}:{} — expected u64, got {:?}",
                path.display(),
                lineno + 1,
                trimmed
            )
        })?;
        set.insert(id);
    }
    Ok(set)
}

pub fn register_tokenizers(index: &Index) {
    index.tokenizers().register(
        "whitespace",
        tantivy::tokenizer::WhitespaceTokenizer::default(),
    );
}

fn segment_first_key(seg: &tantivy::SegmentReader) -> Option<(u64, u64, u64, u64)> {
    let ff = seg.fast_fields();
    let death = ff.u64("death_ah").ok()?;
    let text = ff.u64("text_id").ok()?;
    let part = ff.u64("part_index").ok()?;
    let page = ff.u64("page_id").ok()?;
    (0..seg.max_doc()).find(|&d| !seg.is_deleted(d)).map(|d| {
        (
            death.first(d).unwrap_or(u64::MAX),
            text.first(d).unwrap_or(0),
            part.first(d).unwrap_or(0),
            page.first(d).unwrap_or(0),
        )
    })
}

/// `searchable_segment_ids()` is not in creation order, so sort by each segment's first doc.
pub fn segments_in_reading_order(index: &Index) -> Result<Vec<tantivy::index::SegmentId>> {
    let reader = index.reader()?;
    let searcher = reader.searcher();
    let mut keyed: Vec<((u64, u64, u64, u64), tantivy::index::SegmentId)> = searcher
        .segment_readers()
        .iter()
        .map(|seg| (segment_first_key(seg).unwrap_or((u64::MAX, u64::MAX, u64::MAX, u64::MAX)), seg.segment_id()))
        .collect();
    keyed.sort();
    Ok(keyed.into_iter().map(|(_, id)| id).collect())
}

pub fn merge_all_segments(index: &Index, writer: IndexWriter) -> Result<usize> {
    let mut writer = writer;
    writer.set_merge_policy(Box::new(tantivy::indexer::NoMergePolicy));
    let segment_ids = segments_in_reading_order(index)?;
    let before = segment_ids.len();

    if before > 1 {
        println!("Merging {} segments into one (ordered by first-doc reading key)...", before);
        writer
            .merge(&segment_ids)
            .wait()
            .context("segment merge failed")?;
    } else {
        println!("Index already has {} segment; nothing to merge.", before);
    }

    println!("Garbage-collecting unreferenced files...");
    let gc = writer
        .garbage_collect_files()
        .wait()
        .context("garbage collection failed")?;
    println!("  Deleted {} files", gc.deleted_files.len());

    writer.wait_merging_threads()?;
    Ok(before)
}

pub fn index_size_by_extension(index_dir: &Path) -> Result<(BTreeMap<String, u64>, u64)> {
    let mut by_ext: BTreeMap<String, u64> = BTreeMap::new();
    let mut total = 0u64;
    for entry in fs::read_dir(index_dir)
        .with_context(|| format!("Failed to read index dir {:?}", index_dir))?
    {
        let entry = entry?;
        let meta = entry.metadata()?;
        if !meta.is_file() {
            continue;
        }
        let ext = entry
            .path()
            .extension()
            .and_then(|e| e.to_str())
            .map(|e| format!(".{}", e))
            .unwrap_or_else(|| "(other)".to_string());
        *by_ext.entry(ext).or_insert(0) += meta.len();
        total += meta.len();
    }
    Ok((by_ext, total))
}

pub fn print_size_report(index_dir: &Path) -> Result<()> {
    let (by_ext, total) = index_size_by_extension(index_dir)?;
    println!("Index size by extension ({:?}):", index_dir);
    for (ext, bytes) in &by_ext {
        println!("  {:12} {:>10.3} GB", ext, *bytes as f64 / 1e9);
    }
    println!("  {:12} {:>10.3} GB", "TOTAL", total as f64 / 1e9);
    Ok(())
}

pub fn segment_count(index: &Index) -> Result<usize> {
    Ok(index.searchable_segment_ids()?.len())
}
