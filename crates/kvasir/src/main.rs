#![doc = include_str!("../README.md")]

mod eval;

use std::net::SocketAddr;
use std::process::ExitCode;

use anyhow::Context;
use kvasir_api::Proxies;
use kvasir_config::{LogFormat, Settings};
use kvasir_fetch::Guard;
use tracing::level_filters::LevelFilter;

const USAGE: &str = "usage: kvasir serve | kvasir eval <out> | kvasir openapi | kvasir pdf-text";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["serve"] => exit(serve(Guard::Public)),
        // For the contract tests, whose fake providers and sites listen on this machine.
        ["serve", "--allow-loopback"] => exit(serve(Guard::Loopback)),
        ["eval", rest @ ..] => exit(eval::main(rest)),
        ["openapi"] => {
            println!("{}", kvasir_api::document());
            ExitCode::SUCCESS
        }
        ["pdf-text"] => kvasir_extract::pdf::child(),
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}

fn exit(result: anyhow::Result<()>) -> ExitCode {
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("kvasir: {err:#}");
            ExitCode::FAILURE
        }
    }
}

/// The level a Python logging name means, as `KVASIR_LOG_LEVEL` takes them, upper case.
fn level(name: &str) -> Option<LevelFilter> {
    Some(match name {
        "CRITICAL" | "FATAL" | "ERROR" => LevelFilter::ERROR,
        "WARNING" | "WARN" => LevelFilter::WARN,
        "INFO" => LevelFilter::INFO,
        "DEBUG" => LevelFilter::DEBUG,
        "NOTSET" => LevelFilter::TRACE,
        _ => return None,
    })
}

fn serve(guard: Guard) -> anyhow::Result<()> {
    let mut warnings = Vec::new();
    let env = Settings::from_env(std::env::vars(), &mut warnings)?;
    let level = level(&env.log.level)
        .with_context(|| format!("KVASIR_LOG_LEVEL: {} is not a log level", env.log.level))?;
    let logs = tracing_subscriber::fmt().with_max_level(level);
    match env.log.format {
        LogFormat::Json => logs.json().init(),
        LogFormat::Text => logs.init(),
    }
    for warning in &warnings {
        tracing::warn!("{warning}");
    }
    let proxies = Proxies::parse(std::env::var("FORWARDED_ALLOW_IPS").ok().as_deref());
    let runtime = tokio::runtime::Runtime::new()?;
    runtime.block_on(async {
        let address = format!("{}:{}", env.host, env.port);
        let app = kvasir_api::open(env, guard, proxies, None).await?;
        let listener = tokio::net::TcpListener::bind(&address)
            .await
            .with_context(|| format!("cannot listen on {address}"))?;
        tracing::info!("listening on {address}");
        let service = kvasir_api::router(app).into_make_service_with_connect_info::<SocketAddr>();
        axum::serve(listener, service).with_graceful_shutdown(stopped()).await?;
        Ok(())
    })
}

/// Ctrl-C, or the SIGTERM a container runtime stops the service with.
async fn stopped() {
    let interrupt = async {
        let _ = tokio::signal::ctrl_c().await;
    };
    #[cfg(unix)]
    let terminate = async {
        match tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()) {
            Ok(mut signal) => {
                signal.recv().await;
            }
            Err(_) => std::future::pending().await,
        }
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! {
        () = interrupt => {}
        () = terminate => {}
    }
    tracing::info!("stopping");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_log_level_is_a_python_logging_name() {
        assert_eq!(level("WARNING"), Some(LevelFilter::WARN));
        assert_eq!(level("CRITICAL"), Some(LevelFilter::ERROR));
        assert_eq!(level("INFO"), Some(LevelFilter::INFO));
        // Read as a target by tracing's own syntax, it would silence every line.
        assert_eq!(level("LOUD"), None);
    }
}
