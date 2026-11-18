//! Pre-session snapshots (port of backup.py): full-library JSONL dumps, the
//! last-resort restore source. One JSON object per line under a header line
//! carrying the library version; the file must be byte-identical to Python's,
//! because plans pin themselves to a backup id and restore reads objects back.

use std::collections::HashSet;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

use crate::client::{ItemsQuery, ZoteroClient};
use crate::config::Error;
use crate::pyjson::dumps;
use crate::status::sorted_glob;

pub struct BackupInfo {
    pub path: PathBuf,
    pub timestamp: Value,
    pub library_version: Value,
}

pub struct BackupStats {
    pub items: usize,
    pub collections: usize,
    pub tags: usize,
}

pub fn parse_line(path: &Path, raw: &str) -> Result<Value, Error> {
    serde_json::from_str(raw).map_err(|e| Error::Config(format!("{}: {e}", path.display())))
}

/// The newest backup's identity, read from its filename order and its header
/// line alone – never the whole file, which can run to tens of megabytes.
pub fn latest_backup(backups_dir: &Path) -> Result<Option<BackupInfo>, Error> {
    let Some(path) = sorted_glob(backups_dir, ".jsonl")?.pop() else {
        return Ok(None);
    };
    let mut first = String::new();
    BufReader::new(File::open(&path)?).read_line(&mut first)?;
    let header = parse_line(&path, &first)?;
    Ok(Some(BackupInfo {
        timestamp: header["timestamp"].clone(),
        library_version: header["library_version"].clone(),
        path,
    }))
}

/// Item, collection and distinct-tag counts from one backup file, streamed.
pub fn backup_stats(path: &Path) -> Result<BackupStats, Error> {
    let (mut items, mut collections) = (0, 0);
    let mut tags = HashSet::new();
    for raw in BufReader::new(File::open(path)?).lines() {
        let line = parse_line(path, &raw?)?;
        match line["kind"].as_str() {
            Some("item") => {
                items += 1;
                if let Some(list) = line["object"]["data"]["tags"].as_array() {
                    tags.extend(list.iter().map(|t| t["tag"].to_string()));
                }
            }
            Some("collection") => collections += 1,
            _ => {}
        }
    }
    Ok(BackupStats {
        items,
        collections,
        tags: tags.len(),
    })
}

/// Snapshot the library; `None` is a verified no-op (unchanged since the last one).
pub fn run_backup(client: &mut ZoteroClient, backups_dir: &Path) -> Result<Option<PathBuf>, Error> {
    let previous = latest_backup(backups_dir)?;
    let Some(items) = client.all_items(ItemsQuery {
        include_trashed: true,
        // str(previous.library_version), as Python sends it; null sends no header.
        if_modified_since: previous
            .map(|p| p.library_version)
            .filter(|v| !v.is_null())
            .map(|v| crate::pyjson::py_str(&v)),
        ..Default::default()
    })?
    else {
        return Ok(None);
    };
    let version = json!(client.last_modified_version);
    let collections = client.all_collections()?;
    let tag_colors = client.setting("tagColors")?;

    let stamp = crate::clock::now().format("%Y%m%dT%H%M%SZ").to_string();
    let path = backups_dir.join(format!("{stamp}.jsonl"));
    let mut lines = vec![json!({"kind": "header", "library_version": version, "timestamp": stamp})];
    lines.extend(
        items
            .into_iter()
            .map(|item| json!({"kind": "item", "object": item})),
    );
    lines.extend(
        collections
            .into_iter()
            .map(|c| json!({"kind": "collection", "object": c})),
    );
    if let Some(setting) = tag_colors {
        lines.push(json!({"kind": "setting", "name": "tagColors", "object": setting}));
    }
    // Written aside and renamed into place, so a crashed process never leaves a
    // torn backup for latest_backup to pick up. Like Python, there is no fsync:
    // a power loss can still leave a partial file on some filesystems.
    let tmp = path.with_extension("tmp");
    fs::write(
        &tmp,
        lines.iter().map(|l| dumps(l) + "\n").collect::<String>(),
    )?;
    fs::rename(&tmp, &path)?;
    Ok(Some(path))
}
