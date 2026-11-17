//! zel – Rust spike of zelador's read-only path (issue #47).
//!
//! Ports `zel status` and `zel audit tags|collections`. Behaviour is pinned to
//! the Python CLI by tests/test_rust_differential.py; see rust/README.md.

mod audit;
mod client;
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
    #[arg(long, global = true)]
    verbose: bool,
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
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
        #[arg(long)]
        since: Option<i64>,
        /// Summary object.
        #[arg(long = "json")]
        as_json: bool,
    },
}

/// Test seam: the differential test points the binary at a local fake.
fn api_base() -> String {
    env::var("ZELADOR_API_BASE").unwrap_or_else(|_| DEFAULT_API_BASE.into())
}

fn make_client(verbose: bool) -> Result<ZoteroClient, Error> {
    let creds = config::load_credentials()?;
    ZoteroClient::new(&creds, &api_base(), verbose)
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
