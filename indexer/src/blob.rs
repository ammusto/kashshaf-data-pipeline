//! encoding 1: little-endian u32 per token.
//! encoding 2: varint(n_tokens) || zstd frame (dictionary `token_codec.zstd_dict`)
//!             of n LEB128 ranks; def_id = rank_to_def[rank].

use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{Connection, OptionalExtension};
use zstd::dict::DecoderDictionary;

pub struct BlobCodec {
    rank_to_def: Vec<u32>,
    dict: DecoderDictionary<'static>,
}

impl BlobCodec {
    pub fn load(conn: &Connection) -> Result<Option<Self>> {
        let has: bool = conn
            .query_row(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='token_codec'",
                [],
                |_| Ok(true),
            )
            .optional()?
            .unwrap_or(false);
        if !has {
            return Ok(None);
        }
        let rank: Option<Vec<u8>> = conn
            .query_row("SELECT data FROM token_codec WHERE key='rank_to_def'", [], |r| r.get(0))
            .optional()?;
        let dict: Option<Vec<u8>> = conn
            .query_row("SELECT data FROM token_codec WHERE key='zstd_dict'", [], |r| r.get(0))
            .optional()?;
        match (rank, dict) {
            (Some(r), Some(d)) => Ok(Some(Self {
                rank_to_def: r.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect(),
                dict: DecoderDictionary::copy(&d),
            })),
            _ => Ok(None),
        }
    }

    pub fn decode(&self, encoding: i64, blob: &[u8]) -> Result<Vec<u32>> {
        match encoding {
            1 => decode_raw(blob),
            2 => {
                let (n, pos) = read_leb128(blob, 0)?;
                let n = n as usize;
                if n == 0 {
                    return Ok(Vec::new());
                }
                let mut dec = zstd::bulk::Decompressor::with_prepared_dictionary(&self.dict)
                    .context("zstd init")?;
                let payload = dec.decompress(&blob[pos..], n * 5).context("zstd decompress")?;
                let mut out = Vec::with_capacity(n);
                let mut p = 0;
                for _ in 0..n {
                    let (rank, next) = read_leb128(&payload, p)?;
                    p = next;
                    out.push(
                        *self
                            .rank_to_def
                            .get(rank as usize)
                            .ok_or_else(|| anyhow!("rank {} out of range", rank))?,
                    );
                }
                Ok(out)
            }
            other => bail!("unknown page_tokens encoding {}", other),
        }
    }
}

pub fn decode_raw(blob: &[u8]) -> Result<Vec<u32>> {
    if blob.len() % 4 != 0 {
        bail!("raw blob length {} not a multiple of 4", blob.len());
    }
    Ok(blob.chunks_exact(4).map(|c| u32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect())
}

pub fn read_leb128(buf: &[u8], mut pos: usize) -> Result<(u64, usize)> {
    let mut result = 0u64;
    let mut shift = 0u32;
    loop {
        let b = *buf.get(pos).ok_or_else(|| anyhow!("truncated varint"))?;
        pos += 1;
        result |= ((b & 0x7F) as u64) << shift;
        if b & 0x80 == 0 {
            return Ok((result, pos));
        }
        shift += 7;
        if shift > 63 {
            bail!("varint too long");
        }
    }
}

pub struct TokenSource {
    conn: Connection,
    codec: Option<BlobCodec>,
    has_encoding: bool,
    pub def_to_triple: Vec<u32>,
    pub max_triple_id: u32,
}

impl TokenSource {
    pub fn open(corpus_db: &std::path::Path) -> Result<Self> {
        let conn = Connection::open(corpus_db)
            .with_context(|| format!("opening corpus.db at {:?}", corpus_db))?;
        let codec = BlobCodec::load(&conn)?;
        let has_encoding = {
            let mut stmt = conn.prepare("PRAGMA table_info(page_tokens)")?;
            let names: Vec<String> = stmt.query_map([], |r| r.get::<_, String>(1))?.filter_map(|r| r.ok()).collect();
            names.iter().any(|n| n == "encoding")
        };
        let has_triple: bool = {
            let mut stmt = conn.prepare("PRAGMA table_info(token_definitions)")?;
            let names: Vec<String> = stmt.query_map([], |r| r.get::<_, String>(1))?.filter_map(|r| r.ok()).collect();
            names.iter().any(|n| n == "triple_id")
        };
        if !has_triple {
            bail!("token_definitions has no triple_id column; run build_sqlite_tokens.py --build-triples first");
        }
        let max_def: i64 = conn.query_row("SELECT COALESCE(MAX(id),0) FROM token_definitions", [], |r| r.get(0))?;
        let mut def_to_triple = vec![0u32; max_def as usize + 1];
        let mut max_triple_id = 0u32;
        {
            let mut stmt = conn.prepare("SELECT id, triple_id FROM token_definitions")?;
            let rows = stmt.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, Option<i64>>(1)?)))?;
            for row in rows {
                let (id, t) = row?;
                let t = t.ok_or_else(|| anyhow!("token_definitions.id {} has NULL triple_id", id))? as u32;
                def_to_triple[id as usize] = t;
                max_triple_id = max_triple_id.max(t);
            }
        }
        Ok(Self { conn, codec, has_encoding, def_to_triple, max_triple_id })
    }

    pub fn page_ids(&self, book_id: u64, part_index: u64, page_id: u64) -> Result<Option<Vec<u32>>> {
        let sql = if self.has_encoding {
            "SELECT encoding, token_ids FROM page_tokens WHERE book_id=?1 AND part_index=?2 AND page_id=?3"
        } else {
            "SELECT 1, token_ids FROM page_tokens WHERE book_id=?1 AND part_index=?2 AND page_id=?3"
        };
        let row: Option<(i64, Vec<u8>)> = self
            .conn
            .query_row(sql, rusqlite::params![book_id as i64, part_index as i64, page_id as i64], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .optional()?;
        match row {
            None => Ok(None),
            Some((enc, blob)) => match (&self.codec, enc) {
                (_, 1) => Ok(Some(decode_raw(&blob)?)),
                (Some(c), 2) => Ok(Some(c.decode(2, &blob)?)),
                (None, 2) => bail!("encoding-2 blob but corpus.db has no token_codec"),
                (_, e) => bail!("unknown encoding {}", e),
            },
        }
    }
}
