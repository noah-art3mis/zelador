//! Configuration: credentials, data directory, config.yaml (port of config.py).

use std::env;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

/// Operational failures exit 1, bad input exits 2 – the CLI's contract.
#[derive(Debug)]
pub enum Error {
    Config(String),
    Zotero(String),
    BadInput(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Config(m) | Error::Zotero(m) | Error::BadInput(m) => f.write_str(m),
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        Error::Config(err.to_string())
    }
}

pub struct Credentials {
    pub api_key: String,
    pub user_id: String,
}

#[derive(Default)]
pub struct Config {
    pub citekey_sources: Vec<String>,
    pub style: String,
}

/// The checkout this binary was built from. Python resolves config.yaml and
/// .env relative to its package; a distributed binary has no checkout, which
/// is a design question for the full port (see rust/README.md).
pub fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .expect("crate lives inside the repo")
        .to_path_buf()
}

pub fn config_file() -> PathBuf {
    repo_root().join("config.yaml")
}

pub fn taxonomy_file() -> PathBuf {
    repo_root().join("taxonomy.yaml")
}

pub fn load_credentials() -> Result<Credentials, Error> {
    // Like python-dotenv's default: a missing file is fine, set variables win.
    let _ = dotenvy::from_path(repo_root().join(".env"));
    let read = |name: &str| env::var(name).ok().filter(|v| !v.is_empty());
    let api_key = read("ZOTERO_API_KEY").ok_or_else(|| {
        Error::Config("ZOTERO_API_KEY is not set — add it to .env at the repo root".into())
    })?;
    let user_id = read("ZOTERO_USER_ID").ok_or_else(|| {
        Error::Config("ZOTERO_USER_ID is not set — add it to .env at the repo root".into())
    })?;
    Ok(Credentials { api_key, user_id })
}

/// `$ZELADOR_DATA_DIR`, else the directory platformdirs picks for "zelador".
pub fn data_dir() -> PathBuf {
    if let Some(dir) = env::var_os("ZELADOR_DATA_DIR").filter(|v| !v.is_empty()) {
        return PathBuf::from(dir);
    }
    let base = directories::BaseDirs::new().expect("a home directory");
    if cfg!(windows) {
        // platformdirs defaults appauthor to appname on Windows.
        base.data_local_dir().join("zelador").join("zelador")
    } else {
        base.data_dir().join("zelador")
    }
}

pub fn ensure_dir(subdir: &str) -> Result<PathBuf, Error> {
    let path = data_dir().join(subdir);
    fs::create_dir_all(&path)?;
    Ok(path)
}

pub fn load_config(path: &Path) -> Result<Config, Error> {
    let defaults = Config {
        citekey_sources: vec![],
        style: "apa".into(),
    };
    if !path.exists() {
        return Ok(defaults);
    }
    let text = fs::read_to_string(path)?;
    let raw: serde_yaml_ng::Value = serde_yaml_ng::from_str(&text)
        .map_err(|e| Error::Config(format!("{}: {e}", path.display())))?;
    let map = match raw {
        serde_yaml_ng::Value::Null => return Ok(defaults),
        serde_yaml_ng::Value::Mapping(map) => map,
        _ => {
            return Err(Error::Config(format!(
                "{} must be a YAML mapping",
                path.display()
            )));
        }
    };
    let allowed = ["zotero_data_dir", "citekey_sources", "style"];
    let mut unknown: Vec<String> = map
        .keys()
        .map(yaml_str)
        .filter(|k| !allowed.contains(&k.as_str()))
        .collect();
    if !unknown.is_empty() {
        unknown.sort();
        return Err(Error::Config(format!(
            "unknown config key(s) in {}: {}",
            path.display(),
            unknown.join(", ")
        )));
    }
    let citekey_sources = match map.get("citekey_sources") {
        None | Some(serde_yaml_ng::Value::Null) => vec![],
        Some(serde_yaml_ng::Value::Sequence(seq)) => seq.iter().map(yaml_str).collect(),
        Some(_) => {
            return Err(Error::Config(format!(
                "citekey_sources in {} must be a list of paths/globs",
                path.display()
            )));
        }
    };
    let style = map
        .get("style")
        .map(yaml_str)
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "apa".into());
    Ok(Config {
        citekey_sources,
        style,
    })
}

fn yaml_str(value: &serde_yaml_ng::Value) -> String {
    match value {
        serde_yaml_ng::Value::String(s) => s.clone(),
        serde_yaml_ng::Value::Number(n) => n.to_string(),
        serde_yaml_ng::Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        _ => String::new(),
    }
}
