//! The text layer of a PDF, read in a child process: a file from anywhere can abort on allocation
//! or never finish, and blocking parsing cannot be cancelled.

use std::io::{Read, Write};
use std::path::Path;
use std::process::{ExitCode, Stdio};
use std::sync::LazyLock;
use std::time::Duration;

use regex::Regex;
use tokio::io::AsyncWriteExt;
use tokio::process::Command;

use crate::{MAX_CHARACTERS, head};

/// A file taking longer is not worth a run's wait. The slowest of the damaged copies tried took 2 s.
pub const TIMEOUT: Duration = Duration::from_secs(60);
// The largest of the PDFs tried peaked at 69 MB.
const MAX_MEMORY: u64 = 1 << 30;
// A download stops at 20 MB, and a PDF cut short falls back to its snippet.
const MAX_INPUT: u64 = 20_000_000;

// A word the PDF broke across two lines, as in "compu-\ntation". A compound broken at its own
// hyphen loses that hyphen, which is the rarer case in justified text.
static LINE_BREAK_HYPHEN: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(\w)-[ \t]*\n[ \t]*([a-z])").expect("a valid pattern"));

/// The text of a PDF, page by page until there is enough of it. A page that fails is skipped, as
/// the rest of the file may read.
pub fn text_of(data: Vec<u8>) -> Result<String, String> {
    let document = pdf_oxide::PdfDocument::from_bytes(data).map_err(|e| e.to_string())?;
    let count = document.page_count().map_err(|e| e.to_string())?;
    let mut pages = Vec::new();
    let mut length = 0;
    for page in 0..count {
        if length >= MAX_CHARACTERS {
            break;
        }
        if let Ok(text) = document.extract_text(page) {
            length += text.chars().count();
            pages.push(text);
        }
    }
    let joined = LINE_BREAK_HYPHEN.replace_all(&pages.join("\n"), "$1$2").into_owned();
    let lines: Vec<&str> = kvasir_types::python_lines(head(&joined, MAX_CHARACTERS))
        .into_iter()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .collect();
    if lines.is_empty() {
        return Err("PDF has no text layer".to_owned());
    }
    Ok(lines.join("\n"))
}

/// The `pdf-text` subcommand: a PDF on stdin, its text on stdout, the reason it failed on stderr.
pub fn child() -> ExitCode {
    if let Err(e) = rlimit::Resource::AS.set(MAX_MEMORY, MAX_MEMORY) {
        eprintln!("cannot limit memory: {e}");
        return ExitCode::FAILURE;
    }
    let mut data = Vec::new();
    if let Err(e) = std::io::stdin().take(MAX_INPUT).read_to_end(&mut data) {
        eprintln!("cannot read stdin: {e}");
        return ExitCode::FAILURE;
    }
    match text_of(data).map(|text| std::io::stdout().write_all(text.as_bytes())) {
        Ok(Ok(())) => ExitCode::SUCCESS,
        Ok(Err(e)) => {
            eprintln!("cannot write stdout: {e}");
            ExitCode::FAILURE
        }
        Err(e) => {
            eprintln!("{e}");
            ExitCode::FAILURE
        }
    }
}

/// The text of `data`, from `program args`, which is `kvasir pdf-text` in the service.
pub async fn read(
    program: &Path,
    args: &[&str],
    data: Vec<u8>,
    timeout: Duration,
) -> Result<String, String> {
    let mut child = Command::new(program)
        .args(args)
        // What it parses comes from anywhere, so it holds none of the service's secrets.
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .map_err(|e| format!("cannot start the PDF reader: {e}"))?;
    let mut stdin = child.stdin.take().expect("stdin is piped");
    // Fed while the output is read: both can fill their pipes. A child that stops reading has
    // failed, and says so through its exit.
    let feed = async move {
        let _ = stdin.write_all(&data).await;
    };
    let run = async { tokio::join!(child.wait_with_output(), feed).0 };
    let output = tokio::time::timeout(timeout, run)
        .await
        .map_err(|_| format!("PDF reader took over {}s", timeout.as_secs()))?
        .map_err(|e| format!("PDF reader failed: {e}"))?;
    if !output.status.success() {
        let reason = String::from_utf8_lossy(&output.stderr);
        let reason = reason.lines().next_back().unwrap_or("").trim();
        return Err(if reason.is_empty() {
            format!("PDF reader failed: {}", output.status)
        } else {
            reason.to_owned()
        });
    }
    String::from_utf8(output.stdout).map_err(|_| "PDF reader wrote invalid text".to_owned())
}
