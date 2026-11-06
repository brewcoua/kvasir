//! The kvasir binary.
//!
//! - `kvasir serve` runs the service, configured by `KVASIR_*` variables and `kvasir.db`.
//! - `kvasir openapi` prints the OpenAPI document the web client's types are generated from.
//! - `kvasir pdf-text` reads a PDF on stdin and writes its text; the service runs it as a child.

use std::net::SocketAddr;
use std::process::ExitCode;

use anyhow::Context;
use kvasir_api::Proxies;
use kvasir_config::{LogFormat, Settings};
use kvasir_fetch::Guard;
use tracing_subscriber::EnvFilter;

const USAGE: &str = "usage: kvasir serve | kvasir openapi | kvasir pdf-text";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let args: Vec<&str> = args.iter().map(String::as_str).collect();
    match args.as_slice() {
        ["serve"] => exit(serve(Guard::Public)),
        // For the contract tests, whose fake providers and sites listen on this machine.
        ["serve", "--allow-loopback"] => exit(serve(Guard::Loopback)),
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

fn serve(guard: Guard) -> anyhow::Result<()> {
    let mut warnings = Vec::new();
    let env = Settings::from_env(std::env::vars(), &mut warnings)?;
    let filter = EnvFilter::try_new(env.log.level.to_lowercase())
        .with_context(|| format!("{} is not a log level", env.log.level))?;
    let logs = tracing_subscriber::fmt().with_env_filter(filter);
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
