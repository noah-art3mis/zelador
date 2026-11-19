//! zel – partial Rust port of zelador (SPEC.md, M6).
//!
//! Ports `zel status`, `zel backup`, `zel validate` and `zel audit
//! tags|collections`. Behaviour is pinned to the Python CLI by
//! tests/test_rust_differential.py; see rust/README.md.

mod audit;
mod backup;
mod client;
mod clock;
mod config;
mod contracts;
mod expand;
mod jsondecode;
mod pyjson;
mod pyyaml;
mod status;
mod summary;
mod taxonomy;

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
    /// Check a changeset against taxonomy + hard rules; expand into a version-pinned plan.
    Validate {
        /// Path to a changeset.v1 JSON file.
        changeset: String,
        /// Final outcome object.
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

/// The registry, when the config dir has one; a defective file is an error.
fn registry() -> Result<Option<taxonomy::Taxonomy>, Error> {
    let path = config::taxonomy_file();
    if !path.exists() {
        return Ok(None);
    }
    taxonomy::load_taxonomy(&path).map(Some)
}

fn validate(verbose: bool, changeset: &str, as_json: bool) -> Result<(), Error> {
    let parsed = contracts::load_changeset(changeset)?;
    let Some(info) = backup::latest_backup(&config::ensure_dir("backups")?)? else {
        return Err(Error::Config(
            "no backup exists — run `zel backup` first".into(),
        ));
    };
    let registry = registry()?;
    let mut client = make_client(verbose)?;
    let outcome = expand::expand(
        &parsed,
        &mut client,
        registry.as_ref(),
        info.timestamp,
        clock::now(),
        expand::keygen(),
    )?;
    let plan = match outcome {
        Ok(plan) => plan,
        Err(failures) => {
            for failure in &failures {
                eprintln!("invalid: {failure}");
            }
            if as_json {
                println!(
                    "{}",
                    pyjson::dumps(&json!({"valid": false, "failures": failures}))
                );
            }
            return Err(Error::Exit(2));
        }
    };
    let path = contracts::save_plan(&plan, &config::ensure_dir("plans")?)?;
    if as_json {
        println!(
            "{}",
            pyjson::dumps(&json!({
                "valid": true,
                "plan": plan.id,
                "path": path.to_string_lossy(),
                "operations": plan.operations.len(),
                "groups": plan.intents.len(),
                "settings": plan.settings.is_some(),
                "library_version": plan.library_version,
            }))
        );
    } else {
        for line in summary::summarize(&plan) {
            println!("{line}");
        }
        println!("plan written: {}", path.display());
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
    // Loaded only to refuse a defective registry, as Python does before any
    // check runs; the registry check itself is not ported.
    registry()?;
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

/// The stack the CLI runs on. A changeset may nest `jsondecode::MAX_DEPTH`
/// levels, and parsing, `repr`, serialising and dropping such a value all
/// recurse; the platform main thread (8 MiB on Linux, 1 MiB on Windows) is not
/// sized for that in a debug build.
const STACK_BYTES: usize = 64 << 20;

fn main() -> ExitCode {
    std::thread::Builder::new()
        .stack_size(STACK_BYTES)
        .spawn(run)
        .expect("spawning the CLI thread")
        .join()
        // A panic already printed its message; exit as an uncaught panic does.
        .unwrap_or(ExitCode::from(101))
}

fn run() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Command::Backup { as_json } => backup(cli.verbose, as_json),
        Command::Status { as_json } => status(cli.verbose, as_json),
        Command::Validate { changeset, as_json } => validate(cli.verbose, &changeset, as_json),
        Command::Audit {
            check,
            since,
            as_json,
        } => audit(cli.verbose, check, since, as_json),
    };
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(Error::Exit(code)) => ExitCode::from(code),
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
