//! zel – Rust spike of zelador's read-only path (issue #47).
//!
//! Ports `zel status` and `zel audit tags|collections`. Behaviour is pinned to
//! the Python CLI by tests/test_rust_differential.py; see rust/README.md.

mod audit;
mod backup;
mod client;
mod clock;
mod config;
mod pyjson;
mod status;

use std::env;
use std::process::ExitCode;

use clap::{Parser, Subcommand};
use serde_json::{Value, json};

use client::{DEFAULT_API_BASE, ZoteroClient};
use config::Error;

#[derive(Parser)]
#[command(
    name = "zel",
    about = "Caretaker for a personal Zotero library. The agent proposes; you approve."
)]
struct Cli {
    /// Trace each API request on stderr.
    /// Must precede the command, as in the Python CLI.
    #[arg(long)]
    verbose: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Full-library JSONL snapshot (items incl. trash, collections, tagColors).
    Backup {
        /// Final outcome object.
        #[arg(long = "json")]
        as_json: bool,
    },
    /// One-screen session orientation – the skills' mandatory opener.
    Status {
        /// One status object.
        #[arg(long = "json")]
        as_json: bool,
    },
    /// Run one audit check; write its JSON plus audit-report.md.
    Audit {
        /// The check to run (the spike ports: tags, collections).
        check: Option<String>,
        /// Scope findings to items changed after this library version.
        #[arg(long, allow_negative_numbers = true)]
        since: Option<i64>,
        /// Summary object.
        #[arg(long = "json")]
        as_json: bool,
    },
}

/// Test seam: the differential test points a debug build at a local fake.
/// Release builds always talk to the real API.
fn api_base() -> String {
    if cfg!(debug_assertions)
        && let Ok(base) = env::var("ZELADOR_API_BASE")
    {
        return base;
    }
    DEFAULT_API_BASE.into()
}

fn make_client(verbose: bool) -> Result<ZoteroClient, Error> {
    let creds = config::load_credentials()?;
    ZoteroClient::new(&creds, &api_base(), verbose)
}

fn backup(verbose: bool, as_json: bool) -> Result<(), Error> {
    let backups_dir = config::ensure_dir("backups")?;
    let mut client = make_client(verbose)?;
    let Some(path) = backup::run_backup(&mut client, &backups_dir)? else {
        let info = backup::latest_backup(&backups_dir)?
            .ok_or_else(|| Error::Config("no-op backup without a previous backup".into()))?;
        eprintln!(
            "library unchanged since backup {} — nothing to do",
            pyjson::py_str(&info.timestamp)
        );
        if as_json {
            println!(
                "{}",
                pyjson::dumps(&json!({"noop": true, "library_version": info.library_version}))
            );
        }
        return Ok(());
    };
    let stats = backup::backup_stats(&path)?;
    let version = backup::latest_backup(&backups_dir)?
        .map(|info| info.library_version)
        .unwrap_or(Value::Null);
    if as_json {
        println!(
            "{}",
            pyjson::dumps(&json!({
                "noop": false,
                "path": path.to_string_lossy(),
                "library_version": version,
                "items": stats.items,
                "collections": stats.collections,
                "tags": stats.tags,
            }))
        );
    } else {
        println!(
            "wrote {} — version {}, {} items, {} collections, {} tags",
            path.display(),
            pyjson::py_str(&version),
            stats.items,
            stats.collections,
            stats.tags
        );
    }
    Ok(())
}

fn status(verbose: bool, as_json: bool) -> Result<(), Error> {
    let cfg = config::load_config(&config::config_file())?;
    let backups = config::ensure_dir("backups")?;
    let log = config::ensure_dir("log")?;
    let audit = config::ensure_dir("audit")?;
    let mut result = status::local_status(&backups, &log, &audit, &cfg)?;
    let api = match make_client(verbose).and_then(|mut c| c.library_version()) {
        Ok(version) => json!({"library_version": version, "error": null}),
        Err(err @ (Error::Zotero(_) | Error::Config(_))) => {
            json!({"library_version": null, "error": err.to_string()})
        }
        Err(other) => return Err(other),
    };
    result.insert("api".into(), api);
    if as_json {
        println!("{}", pyjson::dumps(&Value::Object(result)));
    } else {
        for line in status::render_status(&result) {
            println!("{line}");
        }
    }
    Ok(())
}

fn audit(
    verbose: bool,
    check: Option<String>,
    since: Option<i64>,
    as_json: bool,
) -> Result<(), Error> {
    let cfg = config::load_config(&config::config_file())?;
    let check = match check.as_deref() {
        Some(name) if audit::PORTED_CHECKS.contains(&name) => name.to_string(),
        other => {
            return Err(Error::BadInput(format!(
                "the Rust spike ports only the {} checks, not {}",
                audit::PORTED_CHECKS.join(" and "),
                other.map_or("a full run".into(), |n| format!("{n:?}"))
            )));
        }
    };
    let mut client = make_client(verbose)?;
    let summary = audit::run_audit(
        &mut client,
        &config::ensure_dir("audit")?,
        &check,
        since,
        &cfg.style,
    )?;
    if as_json {
        println!("{}", pyjson::dumps(&summary));
        return Ok(());
    }
    if let Some(counts) = summary["counts"].as_object() {
        for (name, count) in counts {
            println!("{:>6}  {name}", count.to_string());
        }
    }
    println!("report: {}", pyjson::py_str(&summary["report"]));
    Ok(())
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Backup { as_json } => backup(cli.verbose, as_json),
        Command::Status { as_json } => status(cli.verbose, as_json),
        Command::Audit {
            check,
            since,
            as_json,
        } => audit(cli.verbose, check, since, as_json),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(err @ Error::BadInput(_)) => {
            eprintln!("error: {err}");
            ExitCode::from(2)
        }
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::from(1)
        }
    }
}
