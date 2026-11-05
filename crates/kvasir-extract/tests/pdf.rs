//! The text layer of a PDF, and the child process that reads it.

use std::path::Path;
use std::time::Duration;

use kvasir_extract::pdf::{read, text_of};

/// A one-page PDF holding `lines`, each drawn on its own line of the page.
fn pdf(lines: &[&str]) -> Vec<u8> {
    let mut content = String::from("BT /F1 12 Tf 72 700 Td 14 TL\n");
    for line in lines {
        content.push_str(&format!("({line}) Tj T*\n"));
    }
    content.push_str("ET\n");
    let objects = [
        "<< /Type /Catalog /Pages 2 0 R >>".to_owned(),
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>".to_owned(),
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R \
         /Resources << /Font << /F1 5 0 R >> >> >>"
            .to_owned(),
        format!("<< /Length {} >>\nstream\n{content}endstream", content.len()),
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>".to_owned(),
    ];
    let mut out = String::from("%PDF-1.4\n");
    let mut offsets = Vec::new();
    for (i, object) in objects.iter().enumerate() {
        offsets.push(out.len());
        out.push_str(&format!("{} 0 obj\n{object}\nendobj\n", i + 1));
    }
    let xref = out.len();
    out.push_str(&format!("xref\n0 {}\n0000000000 65535 f \n", objects.len() + 1));
    for offset in offsets {
        out.push_str(&format!("{offset:010} 00000 n \n"));
    }
    out.push_str(&format!(
        "trailer\n<< /Size {} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n",
        objects.len() + 1
    ));
    out.into_bytes()
}

#[test]
fn a_word_broken_across_lines_is_joined_and_blank_lines_go() {
    let text = text_of(pdf(&["The compu-", "tation of roots", "ends."])).unwrap();
    assert!(text.contains("computation of roots"), "{text}");
    assert!(text.lines().all(|line| !line.trim().is_empty()));
}

#[test]
fn a_damaged_file_is_an_error_not_a_panic() {
    assert!(text_of(b"%PDF-1.4 not really".to_vec()).is_err());
    let mut cut = pdf(&["Some text"]);
    cut.truncate(40);
    assert!(text_of(cut).is_err());
}

#[test]
fn a_pdf_with_no_text_says_so() {
    assert_eq!(text_of(pdf(&[])).unwrap_err(), "PDF has no text layer");
}

const SHELL: &str = "/bin/sh";

#[tokio::test]
async fn the_child_is_fed_and_its_output_read() {
    // More than a pipe holds, in both directions.
    let data = vec![b'a'; 1 << 20];
    let got = read(Path::new(SHELL), &["-c", "cat"], data, Duration::from_secs(10)).await.unwrap();
    assert_eq!(got.len(), 1 << 20);
}

#[tokio::test]
async fn a_child_that_fails_gives_its_reason() {
    let script = "echo 'unreadable PDF' >&2; exit 1";
    let args = ["-c", script];
    let got = read(Path::new(SHELL), &args, vec![0; 1 << 20], Duration::from_secs(10));
    assert_eq!(got.await.unwrap_err(), "unreadable PDF");
}

#[tokio::test]
async fn a_child_that_hangs_is_killed_at_the_timeout() {
    let got = read(Path::new(SHELL), &["-c", "sleep 30"], vec![], Duration::from_millis(100));
    assert!(got.await.unwrap_err().contains("took over"));
}

#[tokio::test]
async fn the_child_holds_no_secret_of_the_parent() {
    // Set by cargo for this test, so a child that inherited the environment would print it.
    let got = read(
        Path::new(SHELL),
        &["-c", "printf %s \"$CARGO_PKG_NAME\""],
        vec![],
        Duration::from_secs(5),
    );
    assert_eq!(got.await.unwrap(), "");
}
