//! Configuration: credentials, data directory, config.yaml (port of config.py).

use std::env;
use std::fmt;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;

use crate::pyjson::{py_str, py_truthy};

/// Operational failures exit 1, bad input exits 2 – the CLI's contract.
#[derive(Debug)]
pub enum Error {
    Config(String),
    Zotero(String),
    BadInput(String),
    /// Already reported on stderr; exit with this code and print nothing more.
    Exit(u8),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Config(m) | Error::Zotero(m) | Error::BadInput(m) => f.write_str(m),
            Error::Exit(code) => write!(f, "exit {code}"),
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

/// `$ZELADOR_CONFIG_DIR`, else platformdirs' `user_config_dir("zelador")`.
pub fn config_dir() -> PathBuf {
    if let Some(dir) = env::var_os("ZELADOR_CONFIG_DIR").filter(|v| !v.is_empty()) {
        return normalise_dir(&dir.to_string_lossy());
    }
    platform_dir("XDG_CONFIG_HOME", ".config")
}

/// platformdirs' rule, which differs from the `directories` crate's: on Linux
/// and macOS a set `$XDG_*_HOME` wins after stripping whitespace, relative
/// values included; otherwise `~/<linux_default>` on Linux and Application
/// Support on macOS. Windows uses the local (not roaming) app data dir, with
/// appauthor defaulting to appname, for config and data alike.
fn platform_dir(xdg_var: &str, linux_default: &str) -> PathBuf {
    let base = directories::BaseDirs::new().expect("a home directory");
    if cfg!(windows) {
        return base.data_local_dir().join("zelador").join("zelador");
    }
    let xdg = env::var(xdg_var).unwrap_or_default();
    let root = match xdg.trim() {
        "" if cfg!(target_os = "macos") => base.home_dir().join("Library/Application Support"),
        "" => base.home_dir().join(linux_default),
        set => PathBuf::from(set),
    };
    normalise_dir(&root.join("zelador").to_string_lossy())
}

pub fn env_file() -> PathBuf {
    config_dir().join(".env")
}

pub fn config_file() -> PathBuf {
    config_dir().join("config.yaml")
}

pub fn taxonomy_file() -> PathBuf {
    config_dir().join("taxonomy.yaml")
}

pub fn load_credentials() -> Result<Credentials, Error> {
    // Like python-dotenv's default: a missing file is fine, set variables win.
    let path = env_file();
    let _ = dotenvy::from_path(&path);
    let read = |name: &str| env::var(name).ok().filter(|v| !v.is_empty());
    let missing =
        |name: &str| Error::Config(format!("{name} is not set — add it to {}", path.display()));
    let api_key = read("ZOTERO_API_KEY").ok_or_else(|| missing("ZOTERO_API_KEY"))?;
    let user_id = read("ZOTERO_USER_ID").ok_or_else(|| missing("ZOTERO_USER_ID"))?;
    Ok(Credentials { api_key, user_id })
}

/// `$ZELADOR_DATA_DIR`, else the directory platformdirs picks for "zelador".
pub fn data_dir() -> PathBuf {
    if let Some(dir) = env::var_os("ZELADOR_DATA_DIR").filter(|v| !v.is_empty()) {
        return normalise_dir(&dir.to_string_lossy());
    }
    platform_dir("XDG_DATA_HOME", ".local/share")
}

/// pathlib's normalisation: drop `.` components and doubled or trailing
/// slashes, keeping `.` itself when nothing else is left.
pub fn normalise_dir(raw: &str) -> PathBuf {
    let path: PathBuf = Path::new(raw)
        .components()
        .filter(|c| !matches!(c, std::path::Component::CurDir))
        .collect();
    if path.as_os_str().is_empty() {
        PathBuf::from(".")
    } else {
        path
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
    let raw = crate::pyyaml::load(&text)
        .map_err(|e| Error::Config(format!("{}: {e}", path.display())))?
        .to_json();
    let map = match raw {
        raw if !py_truthy(&raw) => return Ok(defaults),
        Value::Object(map) => map,
        _ => {
            return Err(Error::Config(format!(
                "{} must be a YAML mapping",
                path.display()
            )));
        }
    };
    let allowed = ["zotero_data_dir", "citekey_sources", "style"];
    let mut unknown: Vec<&str> = map
        .keys()
        .map(String::as_str)
        .filter(|k| !allowed.contains(k))
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
        Some(value) if py_truthy(value) => match value {
            Value::Array(items) => items.iter().map(py_str).collect(),
            _ => {
                return Err(Error::Config(format!(
                    "citekey_sources in {} must be a list of paths/globs",
                    path.display()
                )));
            }
        },
        _ => vec![],
    };
    let style = map
        .get("style")
        .filter(|v| py_truthy(v))
        .map_or_else(|| "apa".into(), py_str);
    Ok(Config {
        citekey_sources,
        style,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn load(text: &str) -> Result<Config, Error> {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let n = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        let path = env::temp_dir().join(format!("zel-config-{}-{n}.yaml", std::process::id()));
        fs::write(&path, text).unwrap();
        let result = load_config(&path);
        fs::remove_file(&path).unwrap();
        result
    }

    #[test]
    fn falsy_citekey_sources_mean_none_like_python() {
        for value in ["\"\"", "{}", "false", "0", "[]", "null"] {
            let cfg = load(&format!("citekey_sources: {value}\n")).unwrap();
            assert!(cfg.citekey_sources.is_empty(), "{value}");
        }
    }

    #[test]
    fn falsy_document_means_defaults_like_python() {
        for text in ["false\n", "0\n", ""] {
            assert_eq!(load(text).unwrap().style, "apa", "{text:?}");
        }
    }

    #[test]
    fn falsy_style_means_apa_like_python() {
        for value in ["false", "0", "\"\"", "null"] {
            assert_eq!(
                load(&format!("style: {value}\n")).unwrap().style,
                "apa",
                "{value}"
            );
        }
        assert_eq!(load("style: chicago\n").unwrap().style, "chicago");
    }

    /// PyYAML reads plain `no` as False (so the default style) and applies
    /// merge keys; quoted 'no' stays text.
    #[test]
    fn yaml_1_1_scalars_and_merge_keys_read_like_pyyaml() {
        assert_eq!(load("style: no\n").unwrap().style, "apa");
        assert_eq!(load("style: 'no'\n").unwrap().style, "no");
        assert_eq!(load("style: 0_0\n").unwrap().style, "apa");
        let cfg = load("base: &b {style: mla}\n").err().unwrap().to_string();
        assert!(cfg.contains("unknown config key(s)"), "{cfg}");
        assert!(load("<<: {style: mla}\n").is_ok_and(|c| c.style == "mla"));
    }

    #[test]
    fn data_dir_override_normalises_like_pathlib() {
        for (raw, expected) in [
            ("./x", "x"),
            (".", "."),
            ("./", "."),
            ("x//y/", "x/y"),
            ("/a/./b/", "/a/b"),
        ] {
            assert_eq!(normalise_dir(raw), PathBuf::from(expected), "{raw}");
        }
    }

    #[test]
    fn non_list_citekey_sources_still_refused() {
        assert!(load("citekey_sources: notes/*.md\n").is_err());
    }
}
